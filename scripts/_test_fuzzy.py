"""Test fuzzy match directly."""
import sys
sys.path.insert(0, "/home/lxc/aimc")

from gateway.router import RouteEngine
from datetime import datetime

eng = RouteEngine.__new__(RouteEngine)
eng._channels_cache = [
    {
        "id": 3,
        "name": "siliconflow_aggregator",
        "prices": [
            {"model": "zai-org/GLM-4.5V", "time_range": "00:00-23:59", "input_price": 1.5, "price_type": "standard"}
        ],
    }
]

result = eng._get_fuzzy_price(eng._channels_cache[0], "glm-4.5v", datetime.now().hour)
print(f"fuzzy result: {result}")