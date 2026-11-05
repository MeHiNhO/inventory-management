"""
Tests for restocking API endpoints.
"""
from datetime import datetime, timedelta

import pytest

from main import restock_orders

# Cost of covering every eligible forecast item, worked out by hand from
# demand_forecasts.json: quantity x unit cost for the seven eligible SKUs.
FULL_RESTOCK_COST = 79822.00
PRIORITY_ORDER = ["WDG-001", "GSK-203", "FLT-405", "BRG-102", "SNR-420", "CTL-330", "VLV-506"]
LARGE_BUDGET = 1000000


@pytest.fixture(autouse=True)
def clear_restock_orders():
    """Reset the shared in-memory order list so tests can't affect each other."""
    restock_orders.clear()
    yield
    restock_orders.clear()


def get_plan(client, budget):
    """Fetch recommendations for a budget and return the parsed body."""
    response = client.get(f"/api/restocking/recommendations?budget={budget}")
    assert response.status_code == 200
    return response.json()


class TestRestockRecommendations:
    """Test suite for the recommendations endpoint."""

    def test_recommendation_structure(self, client):
        """Test that a recommendation has the expected fields."""
        data = get_plan(client, 25000)

        assert data["budget"] == 25000
        for field in ["total_cost", "remaining_budget", "full_restock_cost", "items"]:
            assert field in data

        item = data["items"][0]
        for field in [
            "sku", "name", "quantity", "unit_price", "line_total", "lead_time_days",
            "trend", "current_demand", "forecasted_demand", "on_hand", "on_order", "needed_quantity"
        ]:
            assert field in item

    @pytest.mark.parametrize("budget", [0, 100, 524.79, 5000, 25000, 79822, 200000])
    def test_spend_never_exceeds_budget(self, client, budget):
        """Test that the plan stays within budget and the totals add up."""
        data = get_plan(client, budget)

        assert data["total_cost"] <= budget
        assert sum(item["line_total"] for item in data["items"]) == pytest.approx(data["total_cost"])
        assert data["total_cost"] + data["remaining_budget"] == pytest.approx(budget)

    def test_priority_order(self, client):
        """Test that rising demand comes first, then stable, each by growth."""
        data = get_plan(client, LARGE_BUDGET)
        assert [item["sku"] for item in data["items"]] == PRIORITY_ORDER

    def test_decreasing_demand_is_skipped(self, client):
        """Test that items with falling demand are not recommended."""
        data = get_plan(client, LARGE_BUDGET)
        assert "MTR-304" not in [item["sku"] for item in data["items"]]

    def test_item_covered_by_stock_is_skipped(self, client):
        """Test that PSU-501, with 420 on hand against a forecast of 252, is left out."""
        data = get_plan(client, LARGE_BUDGET)
        assert "PSU-501" not in [item["sku"] for item in data["items"]]

    def test_sku_missing_from_inventory_counts_as_no_stock(self, client):
        """Test that a SKU with no inventory record needs its whole forecast."""
        item = get_plan(client, LARGE_BUDGET)["items"][0]

        assert item["sku"] == "WDG-001"
        assert item["on_hand"] == 0
        assert item["needed_quantity"] == 450

    def test_last_affordable_item_gets_partial_quantity(self, client):
        """Test the fill at $25,000: two full lines, then 739 of 950 filters."""
        data = get_plan(client, 25000)

        assert [(item["sku"], item["quantity"]) for item in data["items"]] == [
            ("WDG-001", 450), ("GSK-203", 600), ("FLT-405", 739)
        ]
        assert data["total_cost"] == 24992.25
        assert data["remaining_budget"] == 7.75

    def test_budget_math_uses_exact_cents(self, client):
        """Test a budget where float floor division would buy 20 units instead of 21."""
        data = get_plan(client, 524.79)

        assert data["items"][0]["quantity"] == 21
        assert data["total_cost"] == 524.79
        assert data["remaining_budget"] == 0

    def test_exact_full_budget_covers_everything(self, client):
        """Test that the full restock cost buys every line in full."""
        data = get_plan(client, FULL_RESTOCK_COST)

        assert data["full_restock_cost"] == FULL_RESTOCK_COST
        assert data["total_cost"] == FULL_RESTOCK_COST
        assert data["remaining_budget"] == 0
        assert all(item["quantity"] == item["needed_quantity"] for item in data["items"])

    def test_zero_budget_recommends_nothing(self, client):
        """Test that a zero budget still reports the full restock cost."""
        data = get_plan(client, 0)

        assert data["items"] == []
        assert data["total_cost"] == 0
        assert data["full_restock_cost"] == FULL_RESTOCK_COST

    @pytest.mark.parametrize("query", ["", "?budget=-1", "?budget=abc", "?budget=inf", "?budget=nan", "?budget=1e308"])
    def test_invalid_budget_is_rejected(self, client, query):
        """Test that missing, negative, non-numeric and non-finite budgets fail validation."""
        response = client.get(f"/api/restocking/recommendations{query}")
        assert response.status_code == 422


class TestRestockOrders:
    """Test suite for submitting and listing restock orders."""

    def test_no_orders_initially(self, client):
        """Test that the order list starts empty."""
        response = client.get("/api/restocking/orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_order(self, client):
        """Test that an order captures the plan for its budget."""
        response = client.post("/api/restocking/orders", json={"budget": 25000})
        assert response.status_code == 201

        order = response.json()
        assert order["order_number"].startswith("RST-")
        assert order["order_number"].endswith("-0001")
        assert order["status"] == "Submitted"
        assert order["budget"] == 25000
        assert order["total_cost"] == 24992.25
        assert [item["sku"] for item in order["items"]] == ["WDG-001", "GSK-203", "FLT-405"]

    def test_order_lines_match_customer_order_shape(self, client):
        """Test that lines carry the fields the Orders page reads, and no planning fields."""
        order = client.post("/api/restocking/orders", json={"budget": 25000}).json()
        line = order["items"][0]

        assert line["name"] == "Industrial Widget Type A"
        assert line["quantity"] == 450
        assert line["unit_price"] == 24.99
        assert "trend" not in line

    def test_lead_time_is_the_slowest_line(self, client):
        """Test that lead time is the longest line, and delivery date follows from it."""
        order = client.post("/api/restocking/orders", json={"budget": 25000}).json()

        assert order["lead_time_days"] == max(item["lead_time_days"] for item in order["items"])
        placed = datetime.fromisoformat(order["order_date"])
        delivery = datetime.fromisoformat(order["expected_delivery"])
        assert delivery - placed == timedelta(days=order["lead_time_days"])

    def test_orders_listed_newest_first(self, client):
        """Test that the most recent order comes first."""
        first = client.post("/api/restocking/orders", json={"budget": 5000}).json()
        second = client.post("/api/restocking/orders", json={"budget": 5000}).json()

        data = client.get("/api/restocking/orders").json()
        assert [order["id"] for order in data] == [second["id"], first["id"]]

    def test_client_cannot_set_prices(self, client):
        """Test that lines sent by the client are ignored."""
        response = client.post("/api/restocking/orders", json={
            "budget": 25000,
            "items": [{"sku": "WDG-001", "quantity": 1, "unit_price": 0.01}]
        })

        assert response.status_code == 201
        assert response.json()["items"][0]["unit_price"] == 24.99

    def test_budget_too_small_returns_400(self, client):
        """Test that a budget below the cheapest unit can't create an empty order."""
        response = client.post("/api/restocking/orders", json={"budget": 8})

        assert response.status_code == 400
        assert "detail" in response.json()
        assert client.get("/api/restocking/orders").json() == []

    @pytest.mark.parametrize("body", [{}, {"budget": 0}, {"budget": -5}, {"budget": "abc"}, {"budget": 1e308}])
    def test_invalid_order_body_is_rejected(self, client, body):
        """Test that missing, zero, negative and oversized budgets fail validation."""
        response = client.post("/api/restocking/orders", json=body)
        assert response.status_code == 422

    def test_placed_order_reduces_recommendations(self, client):
        """Test that ordered quantities are no longer recommended."""
        client.post("/api/restocking/orders", json={"budget": 25000})
        data = get_plan(client, LARGE_BUDGET)
        items = {item["sku"]: item for item in data["items"]}

        assert "WDG-001" not in items
        assert "GSK-203" not in items
        assert items["FLT-405"]["on_order"] == 739
        assert items["FLT-405"]["needed_quantity"] == 211
        assert data["full_restock_cost"] == FULL_RESTOCK_COST - 24992.25

    def test_nothing_left_to_order_after_full_restock(self, client):
        """Test that a second order is refused once everything is covered."""
        client.post("/api/restocking/orders", json={"budget": LARGE_BUDGET})

        data = get_plan(client, LARGE_BUDGET)
        assert data["items"] == []
        assert data["full_restock_cost"] == 0

        response = client.post("/api/restocking/orders", json={"budget": LARGE_BUDGET})
        assert response.status_code == 400

    def test_customer_data_unchanged_by_restock_order(self, client):
        """Test that restock orders stay out of customer orders and revenue."""
        orders_before = client.get("/api/orders").json()
        summary_before = client.get("/api/dashboard/summary").json()

        client.post("/api/restocking/orders", json={"budget": 25000})

        assert client.get("/api/orders").json() == orders_before
        assert client.get("/api/dashboard/summary").json() == summary_before


class TestDemandForecastCostFields:
    """Test suite for the fields restocking adds to forecasts."""

    def test_forecasts_include_cost_and_lead_time(self, client):
        """Test that every forecast has a positive price and lead time."""
        data = client.get("/api/demand").json()

        assert len(data) > 0
        for forecast in data:
            assert forecast["unit_cost"] > 0
            assert forecast["lead_time_days"] > 0
