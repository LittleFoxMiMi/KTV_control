import torch_directml
try:
    count = torch_directml.device_count()
    print(f"Device count: {count}")
    for i in range(count):
        print(f"Device {i}: {torch_directml.device_name(i)}")
except Exception as e:
    print(f"Error: {e}")
