# gps/__init__.py

from gps.interface import GPSDevice, GPSRegistry
from gps.serial_gps import SerialGPSDevice
from gps.mock_gps import MockGPSDevice

# Register pluggable GPS device classes
GPSRegistry.register(SerialGPSDevice)
GPSRegistry.register(MockGPSDevice)
