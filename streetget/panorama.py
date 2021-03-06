from Queue import Queue
from io import BytesIO
from itertools import product
from urllib import urlencode
from struct import Struct
import threading
import json
import re
import requests
import sys
import logging
import numpy as np
from PIL import Image
from numpy import array

import matplotlib as mpl
mpl.use('Agg')                  # avoid Tk window
import matplotlib.pyplot as plt
# NOTE: Tkinter a has problem with multithread. Matplotlib directive use('Agg')
# turns on non-interactive backend and avoids displaying Tk window.

# Headers for URL GET requests, can be used in the future to fool google servers:
headers = {
    'User-agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:42.0) Gecko/20100101 Firefox/42.0'      # may be used later to fool server
}

loger = logging.getLogger('panorama')
loger.setLevel(logging.WARNING)

class Panorama:
    pano_id = None
    meta = None
    time_meta = None
    depthdata = None
    depthmap = None

    def __init__(self, pano_id=None, latlng=None, radius=15):
        if not pano_id and not latlng:
            return;

        self.pano_id = pano_id if pano_id else self.getPanoID(latlng, radius)
        if not self.pano_id:
            return
        self.meta = self.getMeta()
        self.time_meta = self.getTimeMeta()
        pass

    def getPanoID(self, latlng, radius=15):
        """
        Searches the closest panorama given the latlng and retuns its panoID hash
        :param: lalng tuple - float latitude longitude
        :param: radius - search radius in meters
        :returns string - pano_id hash
        """
        # Base URL and headers
        url = 'https://geo0.ggpht.com/cbk'

        # Query parameters (reverse engineered by googling)
        query = {
            'cb_client':    'maps_sv.tactile',
            'authuser':     '0',
            'hl':           'en',
            'output':       'json',
            'll':           '%.6f,%.6f' % latlng,
            'radius':       radius,
        }

        msg = self.requestData(url, query, headers)
        data = json.loads(msg)
        if len(data) is 0:
            return None
        return data['Location']['panoId'].encode('ascii')

    def isValid(self):
        """ Panorama without metadata is not valid """
        return self.meta and len(self.meta) > 0

    def isCustom(self):
        """ Is it custom or google panorama? """
        if not self.isValid():
            return False
        aux = re.match(r'.*Google.*', self.meta['Data']['copyright'])
        return aux is None

    def hasZoom(self, zoom):
        """
        Checks if zomm level is available.
        :param zoom: int [0-5]
        :return: boolean
        """
        if not self.isValid():
            return False
        z = int(self.meta['Location']['zoomLevels'])
        return z >= zoom

    def getSpatialNeighbours(self):
        """
        Reads metadata returned from getMeta() and extracts
        links of adjacent panoramas.
        :return: list - strings of adjacent panoId hashes
        """
        pano_ids = []
        try:
            for x in self.meta['Links']:
                pano_ids.append(x['panoId'])
        except Exception as e:
            w = '%s \t %.6f %.6f \t spatial neighbours not found,\n' \
                '%s: %s' % (
                self.pano_id, self.getGPS()[0], self.getGPS()[1],
                type(e).__name__, str(e)
            )
            loger.warn(w)

        return pano_ids

    def getTemporalNeighbours(self):
        """
        Extracts temporal panorama links from
        timemachine metadata.
        :return: list of tuples (pano_id, (year, month))
        """
        #TODO: tt = (None, None)... return tt, following the same pattern
        # as e.g. getGPS
        try:
            aux = self.time_meta[1][0][5][1]  # interesting part of the meta list

            # Get timestamps of available time machine panoramas
            tstamps = []
            for x in aux[8]:
                tstamps.append(tuple(x[1]))  # year, month

            # Get corresponding panoID hashes
            pano_ids = [''] * len(tstamps)              # empty string list alloc
            for j in range(1, len(tstamps) + 1):
                pano_ids[-j] = aux[3][0][-j][0][1]      # pano_id hash string
        except Exception as e:
            w = '%s \t %.6f %.6f\t temporal neighbours not found\n %s:%s' % (
                    self.pano_id, self.getGPS()[0], self.getGPS()[1],
                    type(e).__name__, str(e)
                )
            loger.warn(w)
            return None

        return zip(pano_ids, tstamps)

    def getGPS(self):
        ll = (None, None)
        try:
            lat = self.meta['Location']['lat']
            lng = self.meta['Location']['lng']
            ll = (float(lat), float(lng))
        except Exception as e:
            msg = '%s no GPS found - %s: %s' % (
                self.pano_id, type(e).__name__, str(e)
            )
            loger.error(msg)
        return ll

    def getDate(self):
        dd = (None, None)
        try:
            dates = self.meta['Data']['image_date']
            ptrn = r'(\d+)-\s*(\d+)'
            m = re.match(ptrn, dates)
            dd = (int(m.groups()[0]), int(m.groups()[1]))
        except Exception as e:
            msg = '%s no date found - %s: %s' % (
                self.pano_id, type(e).__name__, str(e)
            )
            loger.error(msg)
        return dd

    def getAllNeighbours(self):
        """
        Returns a list of both spatial and temporal
        panorama neighbours.
        :return: list  - panoID hashes
        """
        sn = self.getSpatialNeighbours()
        tn = self.getTemporalNeighbours()
        if not tn and not sn:
            return []                           # no neighbours at all
        elif tn and sn:
            return (sn + [x for x,t in tn])     # both temporal and spatial
        elif sn:
            return sn                           # spatial neighbours only
        else:
            return [x for x,t in tn]            # temporal neighbours only

    def getImage(self, zoom=5, n_threads=16):
        """
        Gets panorama image at given zoom level. The image
        consists of image tiles that are fetched and stitched
        together. The resulting image is cropped in order to
        form a spherical panorama.
        :param zoom:
        :param n_threads:
        :return: Image - panorama at given zoom level
        """
        if self.isCustom():
            raise NotImplementedError('Custom panorama is not implemented')

        tw, th = self.numTiles(zoom)
        tiles = tw*th*[None]

        n_threads = min(n_threads, tw*th)

        sentinel = object()
        def worker(q):
            while True:
                item = q.get()
                if item is sentinel:
                    q.task_done()
                    break
                x,y = item
                tiles[y+th*x] = self.getTile(x, y, zoom)
                q.task_done()


        # Starting threads
        q = Queue()

        for x in range(n_threads):
            t = threading.Thread(target=worker, args=(q,))
            t.setDaemon(True)
            t.start()

        # Queueing jobs
        for xy in product(range(tw), range(th)):
            q.put(xy)
        # Queueing sentinels to exit the threads
        for _ in range(n_threads):
            q.put(sentinel)

        q.join()            # all jobs finished

        # Stitching tiles together
        pano = Image.new('RGB',(512*tw, 512*th))
        grid = [xy for xy in product(range(tw), range(th))]

        for x,y in grid:
            pano.paste(tiles[y+th*x], (512*x, 512*y))

        box = self.cropSize(zoom)
        return pano.crop(box)

    def getTile(self, x, y, zoom=5):
        """
        Gets panorama image tile 512x512 at position (x,y)
        :param x: int - tile coordinate horizontal
        :param y: int - tile coordinate vertical
        :param zoom: int [0-5] - zoom level
        :return: Image - panorama tile
        """
        url ='https://geo2.ggpht.com/cbk'
        query = {
                    'output':   'tile',
                    'zoom':     zoom,
                    'x':        x,
                    'y':        y,
                    'panoid':   self.pano_id
                }

        msg = self.requestData(url,query, headers=headers)
        file = BytesIO(msg)
        img = Image.open(file)
        return img
    
    def getDepthData(self):
        encoded = self.meta['model']['depth_map']
        # Decode
        encoded += '=' * (len(encoded) % 4)
        encoded = encoded.replace('-', '+').replace('_', '/')
        data = encoded.decode('base64').decode('zip')       # base64 encoded

        # Read header
        hsize = ord(data[0])                # header size in bytes
        fmt = Struct('< x 3H B')            # little endian, padding byte, 3x unsigned short int, unsigned char
        n_planes, width, height, offset = fmt.unpack(data[:hsize])

        # Read plane labels
        n = width * height
        fmt = Struct('%dB' % n)
        lbls = fmt.unpack(data[offset:offset+fmt.size])
        offset += fmt.size

        # Read planes
        fmt = Struct('< 4f')                # little endian, 4 signed floats
        planes = []
        for i in xrange(n_planes):
            unpacked = fmt.unpack(data[offset:offset+fmt.size])
            planes.append((unpacked[:3], unpacked[3]))
            offset += fmt.size

        self.depthdata = (width, height), lbls, planes
        return self.depthdata
    
    def getDepthImg(self, zoom=None):
        """
        Computes depth image from depth data given by
        getDepthData(). Default image size is 5120x256.
        If zoom is given, the the image is resized to
        correspond to the panorama image size at given
        zoom level.
        :param zoom: int [0-5], default None
        :return img - PIL Image object
        """
        size, lbls, planes = self.depthdata
        w, h = size
        pi = np.pi

        # Rays from camera center in spherical coordinates
        y, x = np.indices((h, w))           # grid of coordinates
        offset = pi/2                       # no idea why not pi,
        yaw = (w-1 - x) * 2*pi / (w-1) + offset
        pitch = (h-1 - y) * pi / (h-1)      # 0 down, pi/2 horizontal, pi up

        # Rays from spherical to cartesian
        v = np.array([
            np.sin(pitch) * np.cos(yaw),
            np.sin(pitch) * np.sin(yaw),
            np.cos(pitch)
        ])
        v = v.transpose(1, 2, 0)

        # w x h x 3 normal, resp. w x h x 1 distance
        n = np.array([planes[i][0] for i in lbls]).reshape((h, w, 3))
        d = np.array([planes[i][1] for i in lbls]).reshape((h, w))
        d[d == 0] = np.nan

        # distance from camera centetr, ray inersection with plane
        self.depthmap = d / np.abs(np.sum(v * n, axis=2))

        try:
            plt.imshow(self.depthmap)
        except Exception as e:
            msg = type(e).__name__ + e.__str__()
            loger.error(self.pano_id + ': ' + msg)
            return Image.new('RGB', (1,1))

        buf = BytesIO()
        plt.imsave(buf, self.depthmap)
        buf.seek(0)
        img = Image.open(buf)

        if zoom:
            _, _, w, h = self.cropSize(zoom)
            img = img.resize((w,h), Image.NEAREST)
        return img

    def saveDepthData(self, fname):
        """
        Saves depth data as JSON in following format:
        data[0] - tuple (width w, height h)
        data[1] - tuple w x h plane labels
        data[2] - tuple of the length of # planes
                  item: ((n_0, n_1, n_2), d) where n_i is
                  a component of planes normal vector and d
                  is its distance from camera center.

        Google depth map is prepresented as a set of 3D planes.
        Hence data[0], data[1] represent a 2D matrix which
        corresponds to a spherical panorama. Each item of the
        matrix is a label of a plane. data[2] represents
        the plane parameters - normal vector and distance.

        :param fname - string, filename
        """
        if not self.depthdata:
            self.getDepthData()

        with open(fname, 'w') as f:
            json.dump(self.depthdata, f)

    def saveDepthImage(self, fname, zoom=None):
        """
        Saves the corresponding depth mpa image using the
        depth map data from getDepthData(). Default image size
        is 512x256. If zoom is given, the the image is resized
        to correspond to the panorama image size at given zoom level.

        :param fname: string file name
        :param zoom: int [0-5], default None
        """
        if not self.depthdata:
            self.getDepthData()

        img = self.getDepthImg(zoom)
        with open(fname, 'w') as f:
            img.save(f, 'JPEG')


    def numTiles(self, zoom):
        """
        Number of image tile for given zoom level. Reverse
        engineered using the 'utilGetNumTiles()' method
        :param zoom: int [0-5] - panorama zoom level
        :return: tuple - #of tiles (horizontally, vertically)
        """
        # Switch
        return [
            (1, 1),
            (2, 1),
            (4, 2),
            (7, 4),
            (13, 7),
            (26, 13)
        ][zoom]

    def cropSize(self, zoom):
        """
        Gives corners of the panorama image crop for given zoom-level.
        Panoramas are composed of 512x512 tiles. After a stitching at some
        zoom levels the bottom is padded by black or the right most edge of
        panorama overlaps the left edge (pano image wraps itself). Hence a
        crop is necessary to be done. The values were reverse-engineered.
        :param zoom: int [0-5] - current zoom level
        :return: tuple - a crop box, top left, btm right corners
        """
        return [
            (0, 0, 417, 208),
            (0, 0, 833, 416),
            (0, 0, 1665, 832),
            (0, 0, 3329, 1664),
            (0, 0, 6656, 3328),
            (0, 0, 13312, 6656)
        ][zoom]

    def getMeta(self):
        """
        Gets raw metadata of the panorama.
        :return: dictionary - data from returned JSON
        """
        if not self.pano_id:
            return None

        url = 'https://cbks1.google.com/cbk'
        query = {
            'output':       'json',
            'v':            4,
            'cb_client':    'apiv3',
            'hl':           'en-US',
            'oe':           'utf-8',
            'dmz':          0,              # depth map uncompressed
            'pmz':          0,              # pano map  uncompressed
            'dm':           1,              # depth map
            'pm':           0,              # pano map
            'panoid':       self.pano_id    # panoID hash
        }

        #TODO: process uncompressed depth. Is it the same as compressed?
        #TODO: what is pano map and how to use it?

        msg = self.requestData(url, query, headers)
        if not msg:
            return None

        jsons = None
        try:
            jsons = json.loads(msg)
        except Exception as e:
            w = '%s has no meta JSON, received: %s' % (self.pano_id, msg)
            loger.warn(w + str(e))

        return jsons

    def getTimeMeta(self):
        """
        Gets raw timemachne metadata the panorama.
        The crazy 'query' string was reverse engineered by
        listening to the network trafic.
        :return: nested list from JSON
        """
        if not self.pano_id:
            return None

        url = 'https://www.google.fr/maps/photometa/v1'
        query = {
            'authuser': 0,
            'hl': 'en',
            'pb':   '!1m1!1smaps_sv.tactile!2m2!1sen!2sfr!3m3!1m2!1e2!2s'
                    + self.pano_id + '!4m17!1e1!1e2!1e3!1e4!1e5!1e6'
                    '!1e8!4m1!1i48!5m1!1e1!5m1!1e2!6m1!1e1!6m1!1e2',
            'output': 'json'
        }

        msg = self.requestData(url, query, headers)      # .js file as string
        if not msg:
            return None

        # Handle a content of the .js file retrieved form the server
        # Here again - reverse engineered. The js file contains
        # nested arrays with some useful info. String is
        # modified such that it can be loaded as JOSN.

        # Trash the first line
        pattern  = r'.+\n(.+)'
        msg = re.match(pattern, msg).groups()[0]
        # Find '[' or ',' followed by ',' and insert 'null' in between
        pattern = r'([\[,])(?=,)'
        msg = re.sub(pattern, r'\1null', msg)

        data = None
        try:
            # Load the JSON nested list
            data = json.loads(msg);
        except Exception as e:
            w = '%s has no time meta JSON, received: %s' % (self.pano_id, msg)
            loger.warn(w + str(e))

        return data

    def saveMeta(self, fname):
        """
        Saves meta data as JSON
        :param fname: string - filename
        """
        with open(fname, 'w') as f:
            json.dump(self.meta, f)

    def saveTimeMeta(self, fname):
        """
        Saves timemachine meta data as JSON
        :param fname: string - filename
        """
        with open(fname, 'w') as f:
            json.dump(self.meta, f)

    def saveImage(self, fname, zoom=5, n_threads=16):
        """
        Fetches panorama image at given zoom-level
        and saves as JPEG.
        :param fname: string - filename
        :param zoom: int [0-5] - zoom-level
        """
        img = self.getImage(zoom, n_threads)
        img.save(fname, 'JPEG')

    def requestData(self, url, query, headers=None):
        """
        Sends GET URL request formed from a base url, a query string
        and headers. Returns whatever this request receives back.
        :param url: string - base URL
        :param query: dictionary - url query paramteres as key-value
        :param headers: dictionary - header parameters as key-value
        :return: dictionary - data from returned JSON
        """
        # URL GET request
        query_str = urlencode(query).encode('ascii')
        u = None
        # Handle loose internet connection via loop
        err = None
        for _ in range(10):
            try:
                u = requests.get(url + "?" + query_str, headers=headers)
            except Exception as e:
                print type(e).__name__ + str(e)
                print 'URL request retry...'
                err = e
            if u:
                break
        else:
            loger.error('%s %s:%s' % (self.pano_id,type(err).__name__, str(err)))
            print 'Panorama loading error'
            return None

        msg = u.content
        return msg

    def _utilGetNumTiles(self, zoom):
        maxx = 0
        maxy = 0
        for x,y in product(range(30), range(20)):
            img = self.getTile(x,y,zoom)
            h = img.histogram()
            if not sum(h[1:]) == 0:
                maxx = x if x>maxx else maxx
                maxy = y if y>maxy else maxy
        print 'Zoom %d: #tiles - horizontally %d   vertically %d' % (zoom, maxx+1, maxy+1)
        return (maxx, maxy)

    def _utilGetCrop(self, img):
        w,h = img.size
        _, _, col, row = img.getbbox()
        a = array(img.rotate(90).convert('L')).astype('int16')

        x = a[0]
        j = w
        val = sys.maxint
        aux = []
        for y in a[-1:int(w*.7):-1]:
            v = sum(abs(x-y))
            aux.append(v)
            if v<val:
                val = v
                col = j
            j -= 1

        print  "Original size: \t\t\t%d \t%d" % (w, h)
        print  "Estimated crop size: \t%d \t%d" % (col, row)
        print '_______________________________________'

    def __str__(self):
        if not self.isValid():
            return 'Panorama not found'

        g = self.getGPS()
        d = self.getDate()
        s = '\n'
        s+= 'latlng: %f, %f\n' % (g[0], g[1])
        s+= 'pano id: %s\n' % self.pano_id
        s+= 'date: %s, %s\n' % (d[0], d[1])
        s+= '\nSaptial neighbours [id]:\n'
        s+= '------------------------\n'

        nbhs = self.getSpatialNeighbours()
        if nbhs:
            for x in nbhs:
                s += x.__str__() + '\n'

        s+= '\nTemporal neighbours [id, year, month]:\n'
        s+= '--------------------------------------\n'
        nbhs = self.getTemporalNeighbours()
        if nbhs:
            for x,t in nbhs:
                s += x.__str__() + ', ' + t.__str__() + '\n'

        return s

if __name__ == '__main__':
    pid = 'flIERJS9Lk4AAAQJKfjPkQ'
    ll0 = (50, 14.41)
    ll0 = (49.503569,13.544345)
    #p = Panorama()
    p = Panorama(latlng=ll0);
    p.getDepthData()
    p.getDepthImg()
    p.getImage(0)
    pass