import os

filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_components", "telegraf_mqtt", "parsers", "generic.py")
with open(filepath, "r") as f:
    content = f.read()

# Issue 1: Fix percentage logic - restrict usage_ and *_util to CPU/GPU measurements
old_percent = """    field_lower = field.lower()
    if "percent" in field_lower or field_lower == "percentage":
        return "%"
    # CPU usage_* fields are percentages (Telegraf cpu plugin)
    if field_lower.startswith("usage_"):
        return "%"
    # diskio io_util is documented as a 0-1 fraction (not percent)
    # but represents utilization percentage
    if measurement == "diskio" and field_lower == "io_util":
        return "%"
    # GPU utilization is a percentage
    if field_lower in {"utilization", "util"} or (field_lower.endswith("_util") and field_lower != "io_util"):
        return "%" """

new_percent = """    field_lower = field.lower()
    # Fields ending with "_format" are formatted strings, not numeric values
    # and should not receive any unit.
    if field_lower.endswith("_format"):
        return None
    if "percent" in field_lower or field_lower == "percentage":
        return "%"
    # CPU usage_* fields are percentages (Telegraf cpu plugin)
    # Only apply to CPU measurements, not to byte counters or other fields.
    if field_lower.startswith("usage_") and measurement in {"cpu"}:
        return "%"
    # diskio io_util is documented as a 0-1 fraction (not percent)
    # but represents utilization percentage
    if measurement == "diskio" and field_lower == "io_util":
        return "%"
    # GPU utilization is a percentage
    # Only apply to GPU measurements, not to CPU usage fields.
    if field_lower in {"utilization", "util"} or (field_lower.endswith("_util") and field_lower != "io_util"):
        if measurement in {"gpu", "nvidia_gpu"}:
            return "%" """

if old_percent in content:
    content = content.replace(old_percent, new_percent)
    print("OK: Updated percentage logic in infer_native_unit")
else:
    print("FAIL: Could not find percentage logic block")
    exit(1)

# Issue 2: Restrict Mbit/s to network measurements only
old_speed = """    if field_lower in {"bitrate", "speed"}:
        return "Mbit/s" """

new_speed = """    if field_lower in {"bitrate", "speed"}:
        if measurement is None:
            return "Mbit/s"
        if measurement in {"net", "interface", "netstat"}:
            return "Mbit/s" """

if old_speed in content:
    content = content.replace(old_speed, new_speed)
    print("OK: Restricted Mbit/s to network measurements")
else:
    print("FAIL: Could not find speed/bitrate line")
    exit(1)

# Issue 3: Move _format suffix guard in infer_device_class
old_dc = """    field_lower = field.lower()
    if "temp_input" in field_lower or "temp" in field_lower or field_lower == "temperature":"""

new_dc = """    field_lower = field.lower()
    # Fields ending with "_format" are formatted strings, not numeric values
    if field_lower.endswith("_format"):
        return None
    if "temp_input" in field_lower or "temp" in field_lower or field_lower == "temperature":"""

if old_dc in content:
    content = content.replace(old_dc, new_dc)
    print("OK: Moved _format suffix guard in infer_device_class")
else:
    print("FAIL: Could not find device_class function start")
    exit(1)

# Write back
with open(filepath, "w") as f:
    f.write(content)

print("All changes completed successfully")
