# render_worker.py

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage
import renderer

class MapRenderWorker(QThread):
    """
    Asynchronous rendering thread that offloads vector graphics and labeling
    from the main thread onto an off-screen QImage canvas.
    """
    render_completed = Signal(QImage)
    
    def __init__(self, width, height, center_x, center_y, scale, map_data, zoom_details, colors, frame_budget):
        super().__init__()
        self.width = width
        self.height = height
        self.center_x = center_x
        self.center_y = center_y
        self.scale = scale
        self.map_data = map_data
        self.zoom_details = zoom_details
        self.colors = colors
        self.frame_budget = frame_budget
        
    def run(self):
        # Render the map onto a QImage in the background
        img = renderer.render_map(
            self.width, self.height,
            self.center_x, self.center_y, self.scale,
            self.map_data, self.zoom_details, self.colors,
            self.frame_budget,
            is_interruption_requested=self.isInterruptionRequested
        )
        # Emit the completed image if rendering was not interrupted
        if img and not self.isInterruptionRequested():
            self.render_completed.emit(img)
