# gps/mock_gps.py

import os
import json
import math
from PySide6.QtCore import QTimer, QPointF
from utils import inverse_mercator
from gps.interface import GPSDevice

CONFIG_FILE = "mock_gps_config.json"

def load_mock_gps_config():
    """
    Loads mock GPS settings (enabled status and speed) from a separate config file.
    Creates a default configuration file if it does not exist.
    """
    default_config = {
        "enabled": True,
        "speed_kmh": 50.0
    }
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(default_config, f, indent=4)
        except Exception as e:
            print("Error writing default mock GPS config:", e)
        return default_config
        
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            # Ensure keys exist
            for k, v in default_config.items():
                if k not in config:
                    config[k] = v
            return config
    except Exception as e:
        print("Error reading mock GPS config:", e)
        return default_config


class MockGPSDevice(GPSDevice):
    """
    GPS device simulating route traversal at a set speed.
    """
    def __init__(self, speed_kmh=50.0):
        super().__init__(f"Mock GPS (Simulation @ {speed_kmh} km/h)")
        self.speed_kmh = speed_kmh
        self.route_points = []
        self.current_segment_idx = 0
        self.distance_along_segment = 0.0
        self.timer = QTimer()
        self.timer.timeout.connect(self._on_timer)
        self.update_interval_sec = 0.5
        
        # Default starting position (e.g. Dublin coordinate)
        self.last_lat = 53.349805
        self.last_lon = -6.260310
        self.last_heading = 0.0
        
    @classmethod
    def discover(cls):
        """
        Loads settings from mock_gps_config.json.
        Returns exactly one MockGPSDevice instance if enabled, or empty list if disabled.
        """
        config = load_mock_gps_config()
        if not config.get("enabled", True):
            return []
        speed = float(config.get("speed_kmh", 50.0))
        return [cls(speed_kmh=speed)]
        
    def start(self) -> bool:
        self.timer.start(int(self.update_interval_sec * 1000))
        self.status_message.emit("Mock GPS simulation started.")
        return True
        
    def stop(self):
        self.timer.stop()
        self.status_message.emit("Mock GPS simulation stopped.")
        
    def update_route(self, route_points):
        """
        Receives the route coordinates and resets simulation back to the start.
        """
        self.route_points = list(route_points) if route_points else []
        self.current_segment_idx = 0
        self.distance_along_segment = 0.0
        
        if self.route_points:
            pt = self.route_points[0]
            self.last_lat, self.last_lon = inverse_mercator(pt.x(), pt.y())
            # Calculate initial heading towards second point if possible
            if len(self.route_points) > 1:
                pt2 = self.route_points[1]
                dx = pt2.x() - pt.x()
                dy = pt2.y() - pt.y()
                self.last_heading = (450.0 - math.degrees(math.atan2(dy, dx))) % 360.0
            else:
                self.last_heading = 0.0
                
            # Emit initial location
            self.position_updated.emit(self.last_lat, self.last_lon, 0.0, self.last_heading, 1)
            
    def _on_timer(self):
        if not self.route_points or len(self.route_points) < 2:
            # If no route is calculated, stay stationary but keep emitting position
            self.position_updated.emit(self.last_lat, self.last_lon, 0.0, self.last_heading, 1)
            return
            
        speed_mps = (self.speed_kmh / 3.6)
        dt = self.update_interval_sec
        step = speed_mps * dt
        
        self.distance_along_segment += step
        
        while self.current_segment_idx < len(self.route_points) - 1:
            p1 = self.route_points[self.current_segment_idx]
            p2 = self.route_points[self.current_segment_idx + 1]
            seg_len = math.sqrt((p2.x() - p1.x())**2 + (p2.y() - p1.y())**2)
            
            if self.distance_along_segment <= seg_len:
                # Interpolate position on the current segment
                ratio = self.distance_along_segment / seg_len if seg_len > 0 else 0.0
                x = p1.x() + ratio * (p2.x() - p1.x())
                y = p1.y() + ratio * (p2.y() - p1.y())
                
                # Calculate heading (degrees clockwise from North)
                dx = p2.x() - p1.x()
                dy = p2.y() - p1.y()
                self.last_heading = (450.0 - math.degrees(math.atan2(dy, dx))) % 360.0
                
                self.last_lat, self.last_lon = inverse_mercator(x, y)
                speed_knots = self.speed_kmh / 1.852
                
                self.position_updated.emit(self.last_lat, self.last_lon, speed_knots, self.last_heading, 1)
                return
            else:
                # Subtract segment length and move to the next segment
                self.distance_along_segment -= seg_len
                self.current_segment_idx += 1
                
        # Reached the destination
        self.timer.stop()
        end_pt = self.route_points[-1]
        self.last_lat, self.last_lon = inverse_mercator(end_pt.x(), end_pt.y())
        self.position_updated.emit(self.last_lat, self.last_lon, 0.0, self.last_heading, 1)
        self.status_message.emit("Simulation reached destination.")
