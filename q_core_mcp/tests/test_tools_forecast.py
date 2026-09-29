import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from api.config import get_settings
from api.tests.test_forecast import plan_data
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def test_forecast_mcp_roundtrip(test_settings,call_tool):
    from api.main import app
    app.dependency_overrides[get_settings]=lambda:test_settings
    server=build_server(QCoreClient(test_settings,transport=httpx.ASGITransport(app=app)))
    try:
        result=call_tool(server,'replace_forecast_plan',{'payload':{'plan':plan_data(),'actor':'test','note':'Synthetic'}})
        assert call_tool(server,'get_forecast_plan',{})['revision']==result['revision']
        projection=call_tool(server,'get_cash_forecast',{'horizon_days':30,'snowball_monthly_cents':25000})
        assert projection['revision']==result['revision']
        assert next(r for r in projection['rules'] if r['key']=='payment')['amount_cents']==25000
        with pytest.raises(ToolError):
            call_tool(server,'replace_forecast_plan',{'payload':{'plan':plan_data(),'actor':'test','note':'Stale'}})
    finally:
        app.dependency_overrides.clear()
