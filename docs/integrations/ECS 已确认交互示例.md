ECS JSON 示例：

## [smt auto][inbound] 从单层货架抓取到扫码平台
```json
{
    "client_request_id": "demo-30",
    "ecs_url": "http://10.24.209.27:8080",
    "device_code": "ARM04",
    "task_type": "PICK_AND_PUT",
    "timeout_ms": 30000,
    "params": {
        "source": {
            "location_id": "STATION_B",
            "location_type": "ONE_LAYER_RACK",
            "rack_id": "110014",
            "bin_id": "A000001568",
            "bin_type": "3",
            "bin_location": "D",
            "bin_cell_location": 7,
            "reel_layer": 1,
            "reel_thickness": 20,
            "reel_diameter": 15,
            "reel_totalthickness": 20
        },
        "target": {
            "location_id": "STATION_SCAN",
            "location_type": "SCAN_PLATFORM"
        }
    }
}
```

## [smt auto][inbound] 从扫码平台抓取到工作位料箱
```json
{
    "client_request_id": "demo-27",
    "ecs_url": "http://10.24.209.27:8080",
    "device_code": "ARM03",
    "task_type": "PICK_AND_PUT",
    "timeout_ms": 30000,
    "params": {
        "source": {
            "location_id": "STATION_SCAN",
            "location_type": "SCAN_PLATFORM"
        },
        "target": {
            "location_id": "STATION_WORK1",
            "location_type": "BIN",
            "bin_type": "3",
            "bin_cell_location": 7,
            "reel_layer": 1,
            "reel_thickness": 20,
            "reel_diameter": 15,
            "reel_totalthickness": 20
        }
    }
}
```
