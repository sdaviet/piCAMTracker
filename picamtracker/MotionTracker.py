# vim: set et sw=4 sts=4 fileencoding=utf-8:
#
# Python Motion Tracker module of the pyCAMTracker package
# Copyright (c) 2017-2018 Axel Barnitzke <barney@xkontor.org>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import (
    unicode_literals,
    print_function,
    division,
    absolute_import,
    )

# Make Py2's str equivalent to Py3's
str = type('')
try:
    range = xrange
except NameError:
    pass

import sys
import threading
from collections import deque
from time import sleep,clock
import numpy as np
import cv2
from math import atan2,hypot,degrees,acos,pi,sqrt
from picamtracker import GPIOPort

#- globals
MAX_TRACKS     = 16

#- sorting functions
def distance(t,p):
    if t.updates == 0:
        return 99999
    # BUG: upper corner as reference point is more stable than center
    px = p[0] #+ p[2] / 2
    py = p[1] #+ p[3] / 2
    return abs(t.cx - px) + abs(t.cy - py)
    #return np.hypot(t.cx - px, t.cy - py)

# for same distance prefer the more mature track
# remember: we sort from smallest to biggest
def weighted_distance(t,p):
    dist = float(distance(t,p))
    if t.updates:
        val = 10.0 * dist + 5.0 / t.updates
    else:
        val = dist
    return(val)

#- sort tracks by number of updates
def by_updates(t):
    return t.updates

class Tracker(threading.Thread):
    """
    Assign points to tracks
    """
    #--------------------------------------------------------------------
    #-- constructor
    #--------------------------------------------------------------------
    def __init__(self, camera, greenLed=None, redLed=None, config=None):
        super(Tracker,self).__init__()
        self.lock = threading.Lock()
        self.config = config
        self.camera = camera
        self.resx = camera.resolution[0]
        self.resy = camera.resolution[1]
        self.greenLEDThread = greenLed
        self.redLEDThread   = redLed
        self.frame  = 0
        self.motion = None
        self.locked = False
        self.maxDist = 15
        self.trackLifeTime = 17

        #- set the image handler
        #Track.image_handler = image_handler

        #- initialize a fixed number of threads (less garbarge collection)
        self.track_pool = []
        for i in range(0,MAX_TRACKS):
            self.track_pool.append(Track(self))

        #- do things according configuration
        if config is not None:
            Track.xCross = config.conf['xCross']
            Track.yCross = config.conf['yCross']
            Track.maxDist = self.maxDist = config.conf['maxDist']
            Track.minCosDelta = config.conf['minCosDelta']
            self.trackLifeTime = config.conf['trackLifeTime']

        #- thread initialisation stuff
        self.event = threading.Event()
        self.event.clear()
        self.q = deque()
        self.terminated = False
        self.daemon = True
        self.start()

    #--------------------------------------------------------------------
    #-- stop LED threads
    #--------------------------------------------------------------------
    #def __del__(self):
        #if self.greenLEDThread:
        #    self.greenLEDThread.terminated = True
        #    self.greenLEDThread.join()
        #if self.redLEDThread:
        #    self.redLEDThread.terminated = True
        #    self.redLEDThread.join()

    #--------------------------------------------------------------------
    #-- callback for 'maxDist' command
    #--------------------------------------------------------------------
    def set_maxDist(self, value):
        if value > 0 and value < 25:
            Track.maxDist = value

    #--------------------------------------------------------------------
    #-- called by picamera after sizes are known
    #--------------------------------------------------------------------
    def setup_sizes(self, rows, cols):
        Track.maxX = cols
        Track.maxY = rows

    #--------------------------------------------------------------------
    #-- release lock
    #--------------------------------------------------------------------
    def releaseLock(self):
        with self.lock:
            self.locked = False
            frame  = self.frame
            motion = self.motion
        return (frame, motion)

    #--------------------------------------------------------------------
    #-- get crossing status
    #--------------------------------------------------------------------
    def getStatus(self):
        with self.lock:
            frame  = self.frame
            motion = self.motion
            self.frame = 0
        return (frame, motion)

    #--------------------------------------------------------------------
    #-- callback for crossing event
    #--------------------------------------------------------------------
    def crossed(self, frame, motion):
        if self.locked:
            print("blocked")
            return False

        with self.lock:
            self.locked = True
            self.camera.request_key_frame()
            if self.greenLEDThread:
                self.greenLEDThread.event.set()
            self.frame  = frame
            self.motion = motion

        return True

    #--------------------------------------------------------------------
    #-- without thread (don't start the thread otherwise index error raised!)
    #--------------------------------------------------------------------
    def Supdate_tracks(self, frame, motion):
        self.update_track_pool(frame, motion)

    #--------------------------------------------------------------------
    #-- queue new points and feed worker
    #--------------------------------------------------------------------
    def update_tracks(self, frame, motion):
        self.q.append([frame,motion])
        self.event.set()

    #--------------------------------------------------------------------
    #-- stop all threading stuff
    #--------------------------------------------------------------------
    def stop(self):
        if self.greenLEDThread:
            self.greenLEDThread.terminated = True
        if self.redLEDThread:
            self.redLEDThread.terminated = True

        self.terminated = True
        self.q.append([0, [[[0,0,0,0],[0,0]]]])

    #--------------------------------------------------------------------
    #-- Thread run function (calls update_track_pool)
    #-- (needs dqueue to be fats enough)
    #--------------------------------------------------------------------
    def run(self):
        while not self.terminated:
            try:
                frame,motion = self.q.popleft()
                self.update_track_pool(frame,motion)
            except IndexError:
                self.event.clear()
                self.event.wait(1)

    #--------------------------------------------------------------------
    #-- offer new points to existing tracks
    #--------------------------------------------------------------------
    def update_track_pool(self, frame, motion):
        # walk through all changes
        self.updated = False
        has_been_tracked = 0x00000000
        min_dist = 0

        for rn,vn in motion:
            #-- search a track for this coordinate
            tracked = 0x00000000
            # >>> debug
            #cx = rn[0] + rn[2] / 2
            #cy = rn[1] + rn[3] / 2
            #print "try: ", cx,cy
            # <<< debug
            #-- sorting by distance really makes sence here
            #for track in sorted(self.track_pool, key=lambda t: distance(t,rn)):
            for track in sorted(self.track_pool, key=lambda t: weighted_distance(t,rn)):
                #-- skip tracks already updated
                if has_been_tracked & track.id:
                    continue

                #-- the rest of the coordinates can be ignored
                dist = distance(track,rn)
                maxDist = max(self.maxDist,max(rn[2],rn[3]))
                if dist > maxDist:
                    break

                # >>> debug
                #print "   [%s]: (%2d) %3d" % (track.name, track.updates,  dist)
                # <<< debug

                #-- check if track takes coordinates
                tracked = track.update(frame,rn,vn)
                if tracked:
                    has_been_tracked |= tracked
                    #print "[%s] updated" % track.name, cx,cy
                    self.updated = True
                    break

            #-- not yet tracked -> find a free slot
            if not tracked:
                for track in self.track_pool:
                    if track.updates == 0:
                        #print "[%s] new" % track.name, cx,cy
                        track.new_track(frame,rn,vn)
                        self.updated = True
                        break

        #-- remove aged tracks
        for track in self.track_pool:
            if track.updates and frame - track.lastFrame > self.trackLifeTime:
                track.reset()

    def showTracks(self, frame, vis):
        for track in self.track_pool:
            track.showTrack(vis,frame)


class Track:
    '''
    track data container
    '''
    track_names = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    numtracks   = 0
    #minCosDelta = 0.707 #cos(2*22.5)
    #minCosDelta = 0.9
    minCosDelta = 0.5 #cos(2*30.0)
    maxDist     = 20.0
    maxLifeTime = 17
    maxX        = 99999
    maxY        = 99999
    xCross      = -1
    yCross      = -1

    #--------------------------------------------------------------------
    #--
    #--------------------------------------------------------------------
    def __init__(self, parent=None):
        # track identification
        index     = Track.numtracks % 32
        Track.numtracks += 1
        self.id   = 1 << index
        self.name = Track.track_names[index]
        self.parent = parent
        # reset track data
        self.updates = 9999
        self.reset()

    #--------------------------------------------------------------------
    #-- reuse this object to to avoid too much garbage collection
    #--------------------------------------------------------------------
    def reset(self):
        if self.updates < 1:
            return

        #print "[%s](%d) reset" % (self.name,self.updates)

        self.updates = 0
        self.tr   = []
        self.re   = [0,0,0,0]
        self.vv   = np.array([0.0,0.0])
        self.cx   = 0
        self.cy   = 0
        self.old_dir  = None
        self.old_dist = None
        self.old_area = None
        self.maxx = 0
        self.maxy = 0
        self.minx = 99999
        self.miny = 99999
        self.progressx  = 0
        self.progressy  = 0
        self.lastFrame = 0
        self.isGrowing = True
        self.cleanCrossings()

    #--------------------------------------------------------------------
    #--
    #--------------------------------------------------------------------
    def cleanCrossings(self):
        self.turnedX   = False
        self.turnedY   = False
        self.crossedX  = False
        self.crossedY  = False

    #--------------------------------------------------------------------
    #-- check if track can be resetted
    #--------------------------------------------------------------------
    def clean(self,frame):
        if self.updates > 0:
            # TODO: keep this status alive for a couple of frames
            #if self.updates > 20 and self.progressx == 0 and self.progressy == 0:
            #    print "[%s](%d) no motion!" % (self.name, self.updates)
            #    self.reset()
            #    return

            if frame - self.lastFrame > Track.maxLifeTime:
                #print "[%s] (%d) clean" % (self.name, self.updates)
                self.reset()

    #--------------------------------------------------------------------
    #-- start a new track
    #--------------------------------------------------------------------
    def new_track(self,frame,rn,vn):

        #self.reset()
        if rn[0] - vn[0] < 0 or rn[1] - vn[1] < 0:
            #print "[%s]: new track rejected (too low)" % self.name
            return 0
        if rn[0] + rn[2] - vn[0] > Track.maxX or rn[1] + rn[3] - vn[1] > Track.maxY:
            #print "[%s]: new track rejected (too high)" % self.name
            return 0

        #cxn  = rn[0]+rn[2]/2 #xn+wn/2
        #cyn  = rn[1]+rn[3]/2 #yn+hn/2

        cxn  = rn[0]
        cyn  = rn[1]

        self.re  = rn
        self.vv  = np.array(vn)
        self.cx  = cxn
        self.cy  = cyn
        self.tr.append([cxn,cyn])
        self.updates = 1
        self.lastFrame = frame

        return self.id

    #--------------------------------------------------------------------
    #-- update growing status
    #--------------------------------------------------------------------
    def updateGrowingStatus(self, rn):
        # does the track expand into any direction?
        maxx = max(self.maxx, rn[0]+rn[2])
        maxy = max(self.maxy, rn[1]+rn[3])
        minx = min(self.minx, rn[0])
        miny = min(self.miny, rn[1])

        # update progress indicators
        if maxx > self.maxx or minx < self.minx:
            self.progressx = True
        else:
            # TURN-X if the area does not expand any more in x direction
            if self.progressx and not self.turnedX:
                self.turnedX   = True
            self.progressx = False

        # update progress indicators
        if maxy > self.maxy or miny < self.miny:
            self.progressy = True
        else:
            # TURN-Y if the area does not expand any more in y direction
            if self.progressy and not self.turnedY:
                self.turnedY   = True
                #print("[%s](%02d) %2d Y-TURN" % (self.name,self.updates,rn[1]))
            self.progressy = False

        self.maxx = maxx
        self.maxy = maxy
        self.minx = minx
        self.miny = miny

        self.isGrowing =  self.progressx or self.progressy

    #--------------------------------------------------------------------
    #-- raise crossing handler in parent class
    #--------------------------------------------------------------------
    def crossed(self):
        if self.parent:
            self.parent.crossed(self.lastFrame, [self.re, self.vv, [self.minx, self.miny, self.maxx, self.maxy]])

    #--------------------------------------------------------------------
    #-- main target: is the object crossing the crossing line?
    #-- TODO: make the same for x direction
    #--------------------------------------------------------------------
    def detectCrossing(self, dx, dy, r):
        delta = 2

        if Track.yCross > 0:
            # track is crossing target line in Y direction
            #  x0,y0 +--------------+
            #        |              |
            #        +--------------+ x1,y1
            #        
            #  v > 0 |          --->|
            #        |              |
            #  v < 0 |<---          |
            #if self.updates > 4 and self.progressy == True and self.maxy-self.miny > 2*r[3] and self.crossedY == False:
            if self.updates > 4 and self.progressy == True and self.crossedY == False:
                # develope indicators
                vy = -self.vv[1] # remember the velocity has wrong direction!
                y0 = r[1]
                y1 = r[1] + r[3]

                # for low speeds take distance as indicator
                if abs(vy) < 0.25:
                    vy = float(dy)

                # this model uses a band of width == delta to detect a crossing event
                #crossedYPositive =  vy >  0.0 and abs(y1-Track.yCross) < delta and self.miny < Track.yCross
                #crossedYNegative =  vy <= 0.0 and abs(y0-Track.yCross) < delta and self.maxy > Track.yCross

                # this model uses a simple >= limit to detect a crossing event
                crossedYPositive =  vy > 0.1 and y1 >= Track.yCross and (y1 - delta ) < Track.yCross and self.miny < Track.yCross
                crossedYNegative =  vy < -0.1 and y0 <= Track.yCross and (y0 + delta) > Track.yCross  and self.maxy > Track.yCross

                if crossedYPositive:
                    print("[%s](%02d) y1:%2d vy:%4.2f dy:%3d CROSSED++++++++++++++++++++" % (self.name,self.updates,y1,vy, dy))
                    self.crossedY = True
                    self.crossed()

                if crossedYNegative:
                    print("[%s](%02d) y0:%2d vy:%4.2f dy:%3d CROSSED--------------------" % (self.name,self.updates,y0,vy, dy))
                    self.crossedY = True
                    self.crossed()


    #--------------------------------------------------------------------
    #-- does the track leave the tracking area?
    #--------------------------------------------------------------------
    def isLeaving(self, dx, dy):
        leavex = self.maxx+dx > Track.maxX or self.minx+dx < 0
        leavey = self.maxy+dy > Track.maxY or self.miny+dy < 0
        return leavex or leavey

    #--------------------------------------------------------------------
    #-- update track data
    #--------------------------------------------------------------------
    def update(self,frame,rn,vn):

        # this is not the place for new tracks
        if self.updates < 1:
            return 0x00000000

        # remove expired tracks
        if frame - self.lastFrame > Track.maxLifeTime:
            self.reset()
            return 0x00000000

        self.lastFrame = frame

        # PiMotionAnalysis.analyse may be called more than once per frame -> double hit?
        #cxn  = rn[0]+rn[2]/2.0
        #cyn  = rn[1]+rn[3]/2.0

        # estimating via the object center produces very much noise. (??? I didn't think much about that)
        # estimation is done via the upper left corner (until I have something better)
        cxn  = rn[0]
        cyn  = rn[1]

        # accept slow moving objects
        if self.cx == cxn and self.cy == cyn and self.vv[0] == vn[0] and self.vv[1] == vn[1]:
            self.updates += 1
            #print "[%s] double hit" % self.name
            return self.id

        # try to append new coordinates to track
        # TODO: use vx/vy to improve accuracy
        dx    = cxn - self.cx
        dy    = cyn - self.cy
        vx    = -vn[0]
        vy    = -vn[1]
        dist  = hypot(dx,dy)
        area  = rn[2] * rn[3]

        if self.old_area is None:
            self.old_area = area

        # reject objects which change area too much from frame to frame
        if area > self.old_area:
            delta_area = area/self.old_area
        else:
            delta_area = self.old_area/area

        self.old_area = area
        found = 0x00000000
        #if delta_area >= 10.0:
        #    print("delta area: %4.2f" % (delta_area))

        # TODO: poor mans perspective
        # big nearby objects may move fast
        # far away objects may move slow
        # max_dist = m*x + b
        max_dist = max(max(rn[2],rn[3]),Track.maxDist)

        # 1. is the new point in range?
        if dist >= 0.0 and dist < max_dist and delta_area < 10.0:
            # 2. is the new point in the right direction?
            # wait track to become mature and then check for angle
            if self.updates >= 3:
                # save first old status
                if self.old_dir is None:
                    dxo = self.cx - self.tr[-2][0]
                    dyo = self.cy - self.tr[-2][1]
                    self.old_dist = hypot(dxo,dyo)
                    self.old_dir  = np.array([dxo,dyo])

                # multiply vector magnitudes
                vlength = self.old_dist * dist
                new_dir = np.array([dx ,dy])
                # accept all directions if one movement vector is zero
                if vlength <= 0.1:
                    cos_delta = 1.0
                else:
                    # if dx/dy are smaller than +/- 1 use vx/vy as vector
                    # TODO: this is slow and ugly :-/
                    if abs(dx) + abs(dy) < 2.0:
                        new_dir = np.array([vx ,vy])
                        dist = hypot(vx, vy)
                        vlength = dist * self.old_dist
                        
                    cos_delta = np.dot(self.old_dir, new_dir) / vlength

                self.old_dir  = new_dir
                self.old_dist = dist
                self.old_area = area
            else:
                cos_delta = 1.0

            # >>> debug
            #print("%s,%5d,%2d,%2d,%2d,%2d,%2d,%4.2f,%4.2f" %
            #      (self.name,frame,self.updates,rn[0],rn[1],rn[2],rn[3],cos_delta,dist))
            # <<< debug
            # reject all tracks out of direction
            #if self.updates < 3 or dist <= 1.1 or abs(cos_delta) > Track.minCosDelta:
            if self.updates < 3 or abs(cos_delta) > Track.minCosDelta:
                #- update base data
                found   = self.id
                self.re = rn
                self.vv = np.array(vn)
                self.cx = cxn
                self.cy = cyn
                self.updates += 1
                self.tr.append([cxn,cyn])
                if(len(self.tr) > 16):
                    del self.tr[0]

                #- is the coverered area still growing?
                self.updateGrowingStatus(rn)

                #- does the track leave the playground?
                if self.isLeaving(dx,dy):
                    self.reset()
                    return self.id

                # crossing status
                self.detectCrossing(dx,dy,rn)

            else:
                #print("[%s] delta-: %4.2f (%4.2f) dx/dy: %4.2f/%4.2f" % (self.name,cos_delta, degrees(acos(cos_delta)),dx,dy))
                #print "     x:%3d->%3d, y:%3d->%3d dist: %4.2f" % (self.vv[0],vn[0], self.vv[1],vn[1],dist)
                ii = 0
        else:
            #if self.updates < 2:
            #print "[%s] dist-: %4.2f" % (self.name,dist)
            ii = 0

        return found

    #--------------------------------------------------------------------
    #--
    #--------------------------------------------------------------------
    def showTrack(self, vis, frame=0, color=(220,0,0)):
        if self.updates > 3:
            ci = ord(self.name) % 3
            if ci == 0:
                color = (220,20,20)
            if ci == 1:
                color = (20,220,20)
            if ci == 2:
                color = (20,20,220)
        else:
            color = (220,220,20)

        r = self.re
        x = 8 * r[0]
        y = 8 * r[1]
        w = 8 * r[2]
        h = 8 * r[3]
        #pts=np.int32(self.tr[-25:]) * 8
        #cv2.polylines(vis, [pts], False, color)

        cv2.putText(vis,self.name,(x-3,y-3),cv2.FONT_HERSHEY_SIMPLEX,0.5,color,2)
        if self.crossedY and self.parent.greenLEDThread.event.isSet():
            cv2.rectangle(vis,(x,y),(x+w,y+h),color,-4)
        else:
            cv2.rectangle(vis,(x,y),(x+w,y+h),color,2)
        cv2.rectangle(vis,(8*self.minx,8*self.miny),(8*self.maxx,8*self.maxy),color,1)
        ###
        #xm = x+w/2
        #ym = y+h/2
        #dx = -4 * self.vv[0]
        #dy = -4 * self.vv[1]
        #xe = int(xm+dx)
        #ye = int(ym+dy)
        #cv2.arrowedLine(vis,(xm,ym),(xe,ye),color,1)

    #--------------------------------------------------------------------
    #--
    #--------------------------------------------------------------------
    def printTrack(self, frame=0):
        if self.progressx != 0 or self.progressy != 0:
            sys.stdout.write("[%s]:" %(self.name))
            for x,y in self.tr[-4:]:
                sys.stdout.write("  %02d,%02d -> " %(x,y))
            print("(#%2d vx:%02d vy:%02d) age:%d" % (self.updates,int(-self.vv[0]),int(-self.vv[1]),frame-self.lastFrame))

if __name__ == '__main__':

    t = Track()
    t1 = Track()
    print("mask: %08x" % t.id)
    t.new_track(1,[8,8,14,4],[0.0,-4.0])
    t1.new_track(1,[1,1,4,4],[3.0,4.0])
    t.update(2,[0,0,5,5],[3.0,4.0])
    #t.update(0x50, 3,3,5,5,3.0,4.0)
    #t.update(0xAA, 0,0,5,5,3.0,4.0)
    #t.update(0xAA, 1,1,5,4,2.0,4.0)
    #t.printAll()
    #t.reset(0x3f)
    #t.printAll()
    tracker = Tracker()
    tracker.setup_sizes(10,10)
    aa = []
    tracker.update_tracks(1, [[[5,5,2,2],[1.,1.]]])
    tracker.update_tracks(2, [[[6,6,2,2],[1.,1.]]])
    tracker.update_tracks(3, [[[7,7,2,2],[1.,1.]]])
    sleep(1)
    tracker.stop()
    tracker.join()