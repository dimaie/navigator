# gps/interface.py

from PySide6.QtCore import QObject, Signal

class GPSDevice(QObject):
    """
    Abstract base class / interface for GPS devices.
    All GPS plugins must inherit from this and register themselves.
    """
    # Signal emitted when position is updated.
    # Args: (latitude, longitude, speed_knots, heading_degrees, quality)
    position_updated = Signal(float, float, float, float, int)
    
    # Signal emitted when a status or error message occurs.
    status_message = Signal(str)

    def __init__(self, name: str):
        super().__init__()
        self._name = name

    def get_name(self) -> str:
        """Returns the user-friendly name of the GPS device."""
        return self._name

    def start(self) -> bool:
        """Starts receiving GPS data (e.g. opens port or starts simulation)."""
        raise NotImplementedError

    def stop(self):
        """Stops receiving GPS data."""
        raise NotImplementedError

    def update_route(self, route_points):
        """
        Optional: Updates the current active navigation route points.
        Used by simulation modules (like MockGPSDevice) to travel along paths.
        route_points is a list of QPointF (Web Mercator coordinates).
        """
        pass

    @classmethod
    def discover(cls):
        """
        Scans the system or environment and returns a list of instantiated devices.
        Returns a list of instances of the subclass.
        """
        raise NotImplementedError


class GPSRegistry:
    """
    Registry for pluggable GPS devices.
    Modules register themselves here, allowing the app to discover them dynamically.
    """
    _registered_devices = []

    @classmethod
    def register(cls, device_class):
        """Registers a GPSDevice subclass."""
        if device_class not in cls._registered_devices:
            cls._registered_devices.append(device_class)

    @classmethod
    def get_available_devices(cls):
        """
        Discovers all available devices from registered classes and returns them.
        """
        devices = []
        for dev_class in cls._registered_devices:
            try:
                discovered = dev_class.discover()
                devices.extend(discovered)
            except Exception as e:
                print(f"Error discovering devices for {dev_class.__name__}: {e}")
        return devices
