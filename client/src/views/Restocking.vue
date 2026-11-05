<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="!plan" class="error">{{ error }}</div>
    <div v-else>
      <!-- Refetches keep the last plan on screen, so the slider is never removed mid-drag -->
      <div class="card">
        <div class="budget-header">
          <label for="budget-slider" class="budget-label">{{ t('restocking.budget') }}</label>
          <span class="budget-value">{{ formatMoney(budget, 0) }}</span>
        </div>
        <input
          id="budget-slider"
          v-model.number="budget"
          class="budget-slider"
          type="range"
          min="0"
          :max="maxBudget"
          :step="BUDGET_STEP"
          :disabled="maxBudget === 0"
          :aria-valuetext="formatMoney(budget, 0)"
        />
        <div class="budget-scale">
          <span>{{ formatMoney(0, 0) }}</span>
          <span>{{ formatMoney(maxBudget, 0) }}</span>
        </div>
      </div>

      <div v-if="placedOrderNumber" class="confirmation" role="status">
        <span>{{ t('restocking.orderPlaced', { orderNumber: placedOrderNumber }) }}</span>
        <router-link to="/orders">{{ t('restocking.viewOrders') }}</router-link>
      </div>
      <div v-if="error" class="error" role="alert">{{ error }}</div>

      <div class="stats-grid">
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.plannedSpend') }}</div>
          <div class="stat-value">{{ formatMoney(plan.total_cost) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remaining') }}</div>
          <div class="stat-value">{{ formatMoney(plan.remaining_budget) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.fullRestock') }}</div>
          <div class="stat-value">{{ formatMoney(plan.full_restock_cost) }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommended') }} ({{ plan.items.length }})</h3>
          <button
            type="button"
            class="place-order"
            :disabled="placing || plan.items.length === 0"
            @click="placeOrder"
          >
            {{ placing ? t('restocking.placing') : t('restocking.placeOrder') }}
          </button>
        </div>

        <div v-if="plan.items.length === 0" class="empty-state">
          {{ plan.full_restock_cost === 0 ? t('restocking.nothingToRestock') : t('restocking.increaseBudget') }}
        </div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('demand.table.sku') }}</th>
                <th>{{ t('demand.table.itemName') }}</th>
                <th>{{ t('demand.table.trend') }}</th>
                <th class="numeric">{{ t('restocking.table.needed') }}</th>
                <th class="numeric">{{ t('restocking.table.orderQuantity') }}</th>
                <th class="numeric">{{ t('restocking.table.unitPrice') }}</th>
                <th class="numeric">{{ t('restocking.table.lineTotal') }}</th>
                <th class="numeric">{{ t('orders.table.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in plan.items" :key="item.sku">
                <td><strong>{{ item.sku }}</strong></td>
                <td>{{ translateProductName(item.name) }}</td>
                <td>
                  <span :class="['badge', item.trend === 'increasing' ? 'info' : 'success']">
                    {{ item.trend === 'increasing' ? t('demand.increasingDemand') : t('demand.stableDemand') }}
                  </span>
                </td>
                <td class="numeric">{{ item.needed_quantity.toLocaleString() }}</td>
                <td class="numeric"><strong>{{ item.quantity.toLocaleString() }}</strong></td>
                <td class="numeric">{{ formatMoney(item.unit_price) }}</td>
                <td class="numeric"><strong>{{ formatMoney(item.line_total) }}</strong></td>
                <td class="numeric">{{ t('restocking.days', { count: item.lead_time_days }) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrencyWithDecimals } from '../utils/currency'

const BUDGET_STEP = 500
const DEFAULT_BUDGET = 25000
const FETCH_DELAY_MS = 200

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n()

    const loading = ref(true)
    const error = ref(null)
    const plan = ref(null)
    const budget = ref(DEFAULT_BUDGET)
    const maxBudget = ref(0)
    const placing = ref(false)
    const placedOrderNumber = ref(null)

    let fetchTimer = null
    let latestRequest = 0

    const formatMoney = (amount, decimals = 2) => {
      return formatCurrencyWithDecimals(amount, currentCurrency.value, decimals)
    }

    const loadPlan = async () => {
      const requestId = ++latestRequest
      try {
        const result = await api.getRestockRecommendations(budget.value)

        // A slow response for an older slider position must not replace a newer one.
        if (requestId !== latestRequest) return

        error.value = null
        plan.value = result
        maxBudget.value = Math.ceil(result.full_restock_cost / BUDGET_STEP) * BUDGET_STEP

        // Placing an order shrinks the full restock cost, which can leave the budget above the new maximum.
        if (budget.value > maxBudget.value) {
          budget.value = maxBudget.value
        }
      } catch (err) {
        if (requestId !== latestRequest) return
        error.value = t('restocking.loadError')
        console.error('Failed to load recommendations:', err)
      } finally {
        if (requestId === latestRequest) {
          loading.value = false
        }
      }
    }

    const placeOrder = async () => {
      try {
        placing.value = true
        error.value = null
        placedOrderNumber.value = null

        // Send the budget of the plan on screen, not the live slider value, which can
        // be ahead of it during the fetch delay. What the user sees is what gets ordered.
        const order = await api.createRestockOrder(plan.value.budget)
        placedOrderNumber.value = order.order_number
        await loadPlan()
      } catch (err) {
        error.value = t('restocking.orderError')
        console.error('Failed to place order:', err)
      } finally {
        placing.value = false
      }
    }

    watch(budget, () => {
      clearTimeout(fetchTimer)
      fetchTimer = setTimeout(loadPlan, FETCH_DELAY_MS)
    })

    onMounted(loadPlan)
    onBeforeUnmount(() => clearTimeout(fetchTimer))

    return {
      t,
      BUDGET_STEP,
      loading,
      error,
      plan,
      budget,
      maxBudget,
      placing,
      placedOrderNumber,
      placeOrder,
      formatMoney,
      translateProductName
    }
  }
}
</script>

<style scoped>
.budget-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 1rem;
  margin-bottom: 0.75rem;
}

.budget-label {
  font-size: 1.125rem;
  font-weight: 700;
  color: #0f172a;
  letter-spacing: -0.025em;
}

.budget-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-slider {
  display: block;
  width: 100%;
  accent-color: #0f172a;
  cursor: pointer;
}

.budget-slider:disabled {
  cursor: not-allowed;
}

.budget-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 0.375rem;
  font-size: 0.813rem;
  color: #64748b;
}

.confirmation {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0.5rem 1rem;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #166534;
  padding: 1rem;
  border-radius: 8px;
  margin-bottom: 1.25rem;
  font-size: 0.938rem;
}

.confirmation a {
  color: #166534;
  font-weight: 600;
}

.place-order {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.625rem 1.25rem;
  font-size: 0.875rem;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
}

.place-order:hover:not(:disabled) {
  background: #334155;
}

.place-order:focus-visible {
  outline: 2px solid #3b82f6;
  outline-offset: 2px;
}

.place-order:disabled {
  background: #e2e8f0;
  color: #64748b;
  cursor: not-allowed;
}

.empty-state {
  text-align: center;
  padding: 2rem;
  color: #64748b;
  font-size: 0.938rem;
}

th.numeric,
td.numeric {
  text-align: right;
}
</style>
