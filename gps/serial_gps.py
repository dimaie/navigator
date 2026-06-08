# gps/serial_gps.py

import os
import sys
import serial
from PySide6.QtCore import QThread, Signal
from gps.interface import GPSDevice

def parse_nmea_coord(value, direction):
    """
    Parses an NMEA coordinate string (DDMM.MMMMM / DDDMM.MMMMM) and cardinal direction
    into a standard decimal degrees float.
    """
    if not value or not direction:
        return None
    try:
        dot_idx = value.find('.')
        if dot_idx < 0:
            return None
        deg_len = dot_idx - 2
        if deg_len < 0:
            return None
        degrees = float(value[:deg_len])
        minutes = float(value[deg_len:])
        decimal = degrees + (minutes / 60.0)
        if direction in ('S', 'W'):
            decimal = -decimal
        return decimal
    except ValueError:
        return None


class SerialGPSReaderThread(QThread):
    """
    Background worker thread to read lines from the serial port.
    Prevents blockages on the main GUI event loop.
    """
    position_parsed = Signal(float, float, float, float, int)
    log_message = Signal(str)
    
    def __init__(self, port, baudrate):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.running = False
        
    def run(self):
        self.running = True
        self.log_message.emit(f"Opening serial port {self.port} at {self.baudrate} baud...")
        try:
            with serial.Serial(self.port, self.baudrate, timeout=1.0) as ser:
                self.log_message.emit(f"Port {self.port} opened successfully. Listening for NMEA sentences...")
                while self.running and not self.isInterruptionRequested():
                    line = ser.readline()
                    if not line:
                        continue
                    try:
                        line_str = line.decode('ascii', errors='ignore').strip()
                    except Exception:
                        continue
                        
                    if not line_str.startswith('$'):
                        continue
                        
                    self.parse_line(line_str)
        except Exception as e:
            self.log_message.emit(f"Serial port error on {self.port}: {e}")
            
    def stop(self):
        self.running = False
        self.requestInterruption()
        self.wait()

    def parse_line(self, line):
        """
        Parses a single NMEA sentence ($GNGGA or $GNRMC, etc.).
        """
        # Strip checksum
        clean_line = line.split('*')[0]
        parts = clean_line.split(',')
        if not parts:
            return
            
        sentence_type = parts[0]
        
        # Match GGA (GPS fix data)
        if sentence_type.endswith("GGA"):
            # Format: $--GGA,time,lat,N/S,lon,E/W,fix_quality,num_sats,hdop,alt,M,...
            if len(parts) >= 6:
                lat_val = parts[2]
                lat_dir = parts[3]
                lon_val = parts[4]
                lon_dir = parts[5]
                
                lat = parse_nmea_coord(lat_val, lat_dir)
                lon = parse_nmea_coord(lon_val, lon_dir)
                
                fix_quality = 0
                if len(parts) >= 7:
                    try:
                        fix_quality = int(parts[6])
                    except ValueError:
                        pass
                
                if lat is not None and lon is not None:
                    # GGA has no speed/heading, default to 0.0
                    self.position_parsed.emit(lat, lon, 0.0, 0.0, fix_quality)
                    
        # Match RMC (Recommended minimum specific GPS data)
        elif sentence_type.endswith("RMC"):
            # Format: $--RMC,time,status,lat,N/S,lon,E/W,speed_knots,track_angle,date,...
            if len(parts) >= 9:
                status = parts[2]
                if status == 'A':  # 'A' = active/valid fix, 'V' = void/warning
                    lat_val = parts[3]
                    lat_dir = parts[4]
                    lon_val = parts[5]
                    lon_dir = parts[6]
                    
                    lat = parse_nmea_coord(lat_val, lat_dir)
                    lon = parse_nmea_coord(lon_val, lon_dir)
                    
                    speed_knots = 0.0
                    try:
                        speed_knots = float(parts[7])
                    except ValueError:
                        pass
                        
                    heading_degrees = 0.0
                    try:
                        heading_degrees = float(parts[8])
                    except ValueError:
                        pass
                        
                    if lat is not None and lon is not None:
                        self.position_parsed.emit(lat, lon, speed_knots, heading_degrees, 1)


class SerialGPSDevice(GPSDevice):
    """
    GPS device representing an external serial UART receiver.
    """
    def __init__(self, port, baudrate=9600, description=None):
        name = f"Serial Port: {port}"
        if description:
            name = f"{description} ({port})"
        super().__init__(name)
        self.port = port
        self.baudrate = baudrate
        self.thread = None
        
    def get_port(self):
        return self.port
        
    def start(self) -> bool:
        if self.thread and self.thread.isRunning():
            return True
            
        self.thread = SerialGPSReaderThread(self.port, self.baudrate)
        self.thread.position_parsed.connect(self.position_updated.emit)
        self.thread.log_message.connect(self.status_message.emit)
        self.thread.start()
        return True
        
    def stop(self):
        if self.thread:
            self.thread.stop()
            self.thread = None
            
    @classmethod
    def discover(cls):
        devices = []
        try:
            import serial.tools.list_ports
            ports = serial.tools.list_ports.comports()
            for p in ports:
                devices.append(cls(p.device, description=p.description))
        except ImportError:
            pass

        # Specially discover hardware UARTs on Linux/Raspberry Pi (e.g. uConsole)
        if sys.platform.startswith("linux"):
            hw_uarts = ["/dev/ttyS0", "/dev/ttyAMA0"]
            for uart in hw_uarts:
                if os.path.exists(uart):
                    # Avoid duplicate listing if already found
                    if not any(d.get_port() == uart for d in devices):
                        desc = f"uConsole UART GPS ({os.path.basename(uart)})"
                        devices.append(cls(uart, description=desc))
                        
        return devices
