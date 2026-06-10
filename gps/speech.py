# gps/speech.py

import os
import re
import math
from PySide6.QtCore import QObject, QUrl, QPointF
from PySide6.QtTextToSpeech import QTextToSpeech
from PySide6.QtMultimedia import QSoundEffect, QMediaPlayer, QAudioOutput
from utils import project_mercator, inverse_mercator

def get_ground_distance(pts):
    """
    Calculates the true earth ground distance along a list of Mercator coordinates.
    """
    if len(pts) < 2:
        return 0.0
    total = 0.0
    for i in range(len(pts) - 1):
        p1, p2 = pts[i], pts[i+1]
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        merc_dist = math.sqrt(dx*dx + dy*dy)
        y_mid = (p1.y() + p2.y()) / 2.0
        scale_factor = 1.0 / math.cosh(y_mid / 6378137.0)
        total += merc_dist * scale_factor
    return total


def project_point_to_segment(p, a, b):
    """
    Projects point p onto the segment ab, returning the projection point and squared distance.
    """
    ab_x = b.x() - a.x()
    ab_y = b.y() - a.y()
    ap_x = p.x() - a.x()
    ap_y = p.y() - a.y()
    
    ab2 = ab_x*ab_x + ab_y*ab_y
    if ab2 < 1e-9:
        return a, ap_x*ap_x + ap_y*ap_y
        
    t = (ap_x*ab_x + ap_y*ab_y) / ab2
    t = max(0.0, min(1.0, t))
    
    proj = QPointF(a.x() + t*ab_x, a.y() + t*ab_y)
    dx = p.x() - proj.x()
    dy = p.y() - proj.y()
    return proj, dx*dx + dy*dy


def get_speakable_action(instruction):
    """
    Returns the exact instruction text from the transcript (stripped of bullet and coordinates)
    to match the transcript exactly.
    """
    text = instruction.strip().lstrip("-").strip()
    # Remove coordinates
    text = re.sub(r"\s*at\s*\([-+]?\d+\.\d+,\s*[-+]?\d+\.\d+\)$", "", text)
    return text


def generate_beep_wav(filename="notification.wav", duration=0.15, frequency=880.0, volume=0.5):
    """
    Generates a clean 16-bit 44.1kHz wave beep file programmatically to ensure
    sound notifications work out of the box without external asset dependencies.
    """
    if os.path.exists(filename):
        return
    import wave
    import struct
    sample_rate = 44100
    num_samples = int(duration * sample_rate)
    try:
        with wave.open(filename, 'wb') as wav_file:
            wav_file.setparams((1, 2, sample_rate, num_samples, 'NONE', 'not compressed'))
            for i in range(num_samples):
                t = float(i) / sample_rate
                sample = volume * math.sin(2.0 * math.pi * frequency * t)
                # Fade out last 1000 samples to prevent clicking
                if i > num_samples - 1000:
                    fade = float(num_samples - i) / 1000.0
                    sample *= fade
                packed_sample = struct.pack('<h', int(sample * 32767.0))
                wav_file.writeframes(packed_sample)
    except Exception as e:
        print("Error generating notification beep:", e)


class TTSManager(QObject):
    """
    Asynchronous Text-To-Speech and Sound Alert coordinator for active navigation tracking.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tts = QTextToSpeech(self)
        
        # Audio alert sources (MP3 with QMediaPlayer, or fallback WAV with QSoundEffect)
        self.mp3_file = os.path.abspath("ding-dong.mp3")
        self.beep_file = os.path.abspath("notification.wav")
        self.use_mp3 = False
        
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)
        
        self.sound_effect = QSoundEffect(self)
        self.sound_effect.setVolume(0.8)
        
        self._refresh_audio_source()
        
        self.navigation_steps = []
        self.notified_distances = {} # step_idx -> set of distance integers
        self.prev_dist_to_turn = None
        self.prev_step_idx = -1
        self.last_speak_time = 0.0
        self.next_action = None
        self.next_action_distance = 0.0
        self.after_next_action = None
        self.after_next_action_distance = 0.0
        
    def _refresh_audio_source(self):
        """Checks if ding-dong.mp3 exists and configures the appropriate player."""
        if os.path.exists(self.mp3_file):
            self.use_mp3 = True
            self.media_player.setSource(QUrl.fromLocalFile(self.mp3_file))
        else:
            self.use_mp3 = False
            generate_beep_wav(self.beep_file)
            self.sound_effect.setSource(QUrl.fromLocalFile(self.beep_file))
        
    def reset(self):
        """Resets the notified states."""
        self.navigation_steps = []
        self.notified_distances = {}
        self.prev_dist_to_turn = None
        self.prev_step_idx = -1
        self.last_speak_time = 0.0
        self.next_action = None
        self.next_action_distance = 0.0
        self.after_next_action = None
        self.after_next_action_distance = 0.0
        if self.tts.state() == QTextToSpeech.State.Speaking:
            self.tts.stop()
            
    def align_tracking_baseline(self):
        """
        Forces the next GPS update to re-initialize the baseline distance to turn,
        preventing any deferred/stale notifications from triggering.
        """
        self.prev_dist_to_turn = None
        self.prev_step_idx = -1
            
    def play_beep(self):
        """Plays the notification alert sound."""
        self._refresh_audio_source()
        if self.use_mp3:
            self.media_player.stop()
            self.media_player.play()
        else:
            if self.sound_effect.isLoaded():
                self.sound_effect.play()
            
    def speak(self, text):
        """Stops current speech and begins speaking the text asynchronously."""
        if self.tts.state() == QTextToSpeech.State.Speaking:
            self.tts.stop()
        self.tts.say(text)
        
    def set_route_directions(self, directions, route_points):
        """
        Parses instructions and pre-projects their target coordinates to find segment indices.
        """
        self.reset()
        
        for idx, instr in enumerate(directions):
            # Parse coordinate
            match = re.search(r"\(([-+]?\d+\.\d+),\s*([-+]?\d+\.\d+)\)", instr)
            if match:
                lat, lon = float(match.group(1)), float(match.group(2))
                mx, my = project_mercator(lat, lon)
                junc_pt = QPointF(mx, my)
                
                # Snaps junction coordinates to the route segment indexes
                junc_idx = -1
                min_d2 = float('inf')
                for p_idx, pt in enumerate(route_points):
                    d2 = (pt.x() - junc_pt.x())**2 + (pt.y() - junc_pt.y())**2
                    if d2 < min_d2:
                        min_d2 = d2
                        junc_idx = p_idx
                        
                speakable_action = get_speakable_action(instr)
                self.navigation_steps.append({
                    "index": idx,
                    "junc_pt": junc_pt,
                    "junc_idx": junc_idx,
                    "speakable_action": speakable_action,
                    "original_text": instr
                })
                
    def find_closest_route_index(self, route_points, gps_pt):
        min_dist2 = float('inf')
        best_idx = 0
        best_proj = route_points[0]
        for i in range(len(route_points) - 1):
            proj, d2 = project_point_to_segment(gps_pt, route_points[i], route_points[i+1])
            if d2 < min_dist2:
                min_dist2 = d2
                best_idx = i
                best_proj = proj
        return best_idx, best_proj

    def update_navigation(self, gps_pt, route_points, active_profile, tts_mode="full"):
        """
        Tracks vehicle progression against the next navigation step.
        Spoken notifications or beep alerts are triggered when transitioning across distance thresholds.
        Visual HUD state (next_action, after_next_action) is always updated regardless of tts_mode.
        """
            
        if not route_points or not self.navigation_steps:
            return
            
        # 1. Snap GPS coordinate to route
        gps_idx, snapped_gps = self.find_closest_route_index(route_points, gps_pt)
        
        # 2. Find the first upcoming turn ahead of theSnapped index
        next_step = None
        next_step_idx = -1
        for i, step in enumerate(self.navigation_steps):
            if step["junc_idx"] > gps_idx:
                next_step = step
                next_step_idx = i
                break
                
        if not next_step:
            return
            
        # 3. Calculate distance to the turn junction
        junc_idx = next_step["junc_idx"]
        pts_remaining = [snapped_gps] + route_points[gps_idx + 1 : junc_idx + 1]
        dist_to_turn = get_ground_distance(pts_remaining)
        
        # Update current action and distance info for MapWidget visualization
        self.next_action = next_step
        self.next_action_distance = dist_to_turn
        
        self.after_next_action = None
        self.after_next_action_distance = 0.0
        if next_step_idx + 1 < len(self.navigation_steps):
            step_after = self.navigation_steps[next_step_idx + 1]
            junc_idx_after = step_after["junc_idx"]
            pts_between = route_points[junc_idx : junc_idx_after + 1]
            dist_between = get_ground_distance(pts_between)
            self.after_next_action = step_after
            self.after_next_action_distance = dist_to_turn + dist_between
        
        # Reset tracking parameters if the turn has changed
        if next_step_idx != self.prev_step_idx:
            self.prev_step_idx = next_step_idx
            self.prev_dist_to_turn = dist_to_turn
            return
            
        if self.prev_dist_to_turn is None:
            self.prev_dist_to_turn = dist_to_turn
            return
            
        # 4. Skip audio notifications if TTS is disabled
        if tts_mode == "disabled":
            self.prev_dist_to_turn = dist_to_turn
            return

        # 5. Fetch the target notification distances from profile configuration
        target_distances = active_profile.get("tts_distances", [500, 200, 50])
        target_distances = sorted(list(target_distances), reverse=True)
        
        # 6. Detect crossed thresholds
        crossed_thresholds = []
        if next_step_idx not in self.notified_distances:
            self.notified_distances[next_step_idx] = set()
            
        for threshold in target_distances:
            # Check if we transitioned from above to below the threshold in this step
            if self.prev_dist_to_turn > threshold and dist_to_turn <= threshold:
                if threshold not in self.notified_distances[next_step_idx]:
                    crossed_thresholds.append(threshold)
                    
        if crossed_thresholds:
            # Select the smallest (closest/most recent) crossed threshold
            chosen_threshold = min(crossed_thresholds)
            
            # Check if we skipped any larger crossed thresholds in this single hop
            skipped = len(crossed_thresholds) > 1
            
            # Also mark all crossed thresholds as notified so we don't repeat them
            for t in crossed_thresholds:
                self.notified_distances[next_step_idx].add(t)
                
            # Only turns/roundabout actions must be announced
            action_text = next_step.get("speakable_action", "")
            is_turn = any(k in action_text.lower() for k in ["turn", "bear", "roundabout", "sharp"])
            
            if is_turn:
                if tts_mode == "sound_only":
                    self.play_beep()
                elif tts_mode == "full":
                    # Check if TTS is busy (either active speaking or within cooldown)
                    import time
                    current_time = time.time()
                    cooldown = 6.0  # seconds required to notify in full
                    is_speaking = (self.tts.state() == QTextToSpeech.State.Speaking)
                    in_cooldown = (current_time - self.last_speak_time < cooldown)
                    is_busy = is_speaking or in_cooldown
                    
                    # Play beep if busy or if a threshold was skipped
                    if is_busy or skipped:
                        self.play_beep()
                        
                    if not is_busy:
                        # Say the instruction
                        text = f"In {int(chosen_threshold)} meters, {next_step['speakable_action']}"
                        self.speak(text)
                        self.last_speak_time = current_time
            
        self.prev_dist_to_turn = dist_to_turn
