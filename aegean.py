#!/usr/bin/env python
"""
The Aegean source finding program.

Created by:
Paul Hancock
May 13 2011

Modifications by:
Paul Hancock
Jay Banyer
"""

#standard imports
import sys, os
import pyfits
import numpy as np
from scipy.special import erf
import math
import pywcs

#logging and nice options
import logging
from optparse import OptionParser

#external programs
from AegeanTools.fits_image import FitsImage, Beam
from AegeanTools.mpfit import mpfit
from AegeanTools.convert import ra2dec, dec2dec, dec2hms, dec2dms, gcd, bear, translate
import AegeanTools.flags as flags
import AegeanTools.pprocess as pprocess

import multiprocessing
from scipy import ndimage as ndi
from scipy import stats

version='$Revision$'

header="""#Aegean version {0}
# on dataset: {1}"""

#global constants
fwhm2cc = 1/(2*math.sqrt(2*math.log(2)))
cc2fwhm = (2*math.sqrt(2*math.log(2)))

#Note this is now done in Imports.flags but repeated here for reference
## Set some bitwise logic for flood routines
#PEAKED = 1    # 001
#QUEUED = 2    # 010
#VISITED = 4   # 100
#
## Err Flags for fitting routines
#FITERRSMALL   = 1 #00001
#FITERR        = 2 #00010
#FIXED2PSF     = 4 #00100
#FIXEDCIRCULAR = 8 #01000
#NOTFIT        =16 #10000
####################################### CLASSES ################################
##########

class Island():
    """
    A collection of pixels within an image.
    An island is generally composed of one or more components which are
    detected a characterised by Aegean

    Island(pixels,pixlist)
    pixels = an np.array of pixels that make up this island
    pixlist = a list of [(x,y,flux),... ] pixel values that make up this island
              This allows for non rectangular islands to be used. Pixels not lis
              ted
              are set to np.NaN and are ignored by Aegean.
    """
    def __init__(self,pixels=None,pixlist=None):
        if pixels is not None:
            self.pixels=pixels
        elif pixlist is not None:
            self.pixels=self.list2map(pixlist)
        else:
            self.pixels=self.gen_island(64,64)
            
    def __repr__(self):
        return "An island of pixels of shape {0},{1}".format(*self.pixels.shape)
        
    def list2map(self,pixlist):
        """
        Turn a list of (x,y) tuples into a 2d array
        returns: map[x,y]=self.pixels[x,y] and the offset coordinates
        for the new island with respect to 'self'.
        
        Input:
        pixlist - a list of (x,y) coordinates of the pixels of interest

        Return:
        pixels  - a 2d array with the pixels specified (and filled with NaNs)
        xmin,xmax,ymin,ymax - the boundaries of this box within self.pixels
        """
        xmin,xmax= min([a[0] for a in pixlist]), max([a[0] for a in pixlist])
        ymin,ymax= min([a[1] for a in pixlist]), max([a[1] for a in pixlist])
        pixels=np.ones(self.pixels[xmin:xmax+1,ymin:ymax+1].shape)*np.NaN
        for x,y in pixlist:
            pixels[x-xmin,y-ymin]=self.pixels[x,y]
        return pixels,xmin,xmax,ymin,ymax
            
    def map2list(self,map):
        """Turn a 2d array into a list of (val,x,y) tuples."""
        list=[(map[x,y],x,y) for x in range(map.shape[0]) for y in range(map.shape[1]) ]
        return list

    def get_pixlist(self,clip):
        # Jay's version
        indices = np.where(self.pixels > clip)
        ax, ay = indices
        pixlist = [(self.pixels[ax[i],ay[i]],ax[i],ay[i]) for i in range(len(ax))]
        return pixlist
        
    def gauss(self,a,x,fwhm):
        c=fwhm/(2*math.sqrt(2*math.log(2)))
        return a*math.exp( -x**2/(2*c**2) )

    def gen_island(self,nx,ny):
        """
        Generate an island with a single source in it
        Good for testing
        """
        fwhm_x=(nx/8)
        fwhm_y=(ny/4)
        midx,midy =math.floor(nx/2),math.floor(ny/2)    
        source=np.array( [[self.gauss(1,(x-midx),fwhm_x)*self.gauss(1,(y-midy),fwhm_y) for y in range(ny)]
                          for x in range(nx)] )
        source=source*(source>1e-3)
        return source

    def get_pixels(self):
        return self.pixels

class SimpleSource():
    """
    A (forced) measurement of flux at a given location
    """
    header ="#RA           DEC          Flux      err"+\
    "#                        Jy/beam   Jy/beam"+\
    "#==========================================="

    formatter = "{0.ra:11.7f} {0.dec:11.7f} {0.peak_flux: 8.6f} {0.err_peak_flux: 8.6f}"
    
    def __init__(self):
        self.background = None
        self.local_rms = None
        self.ra = None
        self.dec = None
        self.peak_flux = None
        self.err_peak_flux = None
        

    
    def sanitise(self):
        '''
        Convert various numpy types to np.float64 so that they will print properly
        '''
        for k in self.__dict__:
            if type(self.__dict__[k])in[np.float32,np.int16,np.int32,np.int64]:
                self.__dict__[k]=np.float64(self.__dict__[k])
                
    def __str__(self):
        '''
        convert to string
        '''
        self.sanitise()
        return self.formatter.format(self)
   
class OutputSource(SimpleSource):
    """
    Each source that is fit by Aegean is cast to this type.
    The parameters of the source are stored, along with a string
    formatter that makes printing easy. (as does OutputSource.__str__)
    """
     #header for the output   
    header="#isl,src   bkg       rms         RA           DEC         RA         err         DEC        err         Peak      err     S_int     err        a    err    b    err     pa   err   flags\n"+\
    "#         Jy/beam   Jy/beam                               deg        deg         deg        deg       Jy/beam   Jy/beam    Jy       Jy         ''    ''    ''    ''    deg   deg   NCPES\n"+\
    "#=========================================================================================================================================================================================="

    #formatting strings for making nice output
    formatter = "({0.island:04d},{0.source:02d}) {0.background: 8.6f} {0.local_rms: 8.6f} "+\
                "{0.ra_str:12s} {0.dec_str:12s} {0.ra:11.7f} {0.err_ra: 9.7f} {0.dec:11.7f} {0.err_dec: 9.7f} "+\
                "{0.peak_flux: 8.6f} {0.err_peak_flux: 8.6f} {0.int_flux: 8.6f} {0.err_int_flux: 8.6f} "+\
                "{0.a:5.2f} {0.err_a:5.2f} {0.b:5.2f} {0.err_b:5.2f} "+\
                "{0.pa:6.1f} {0.err_pa:5.1f}   {0.flags:05b}"
    
    def __init__(self):
        self.island = None # island number
        self.source = None # source number
        #background = None # local background zero point
        #local_rms= None #local image rms
        self.ra_str = None #str
        self.dec_str = None #str
        #ra = None # degrees
        self.err_ra = None # degrees
        #dec = None # degrees
        self.err_dec = None
        #peak_flux = None # Jy/beam
        #err_peak_flux = None # Jy/beam
        self.int_flux = None #Jy
        self.err_int_flux= None #Jy
        self.a = None # major axis (arcsecs)
        self.err_a = None # arcsecs
        self.b = None # minor axis (arcsecs)
        self.err_b = None # arcsecs
        self.pa = None # position angle (degrees - WHAT??)
        self.err_pa = None # degrees
        self.flags = None
    

    def __str__(self):
        self.sanitise()
        return self.formatter.format(self)
    
    def as_list(self):
        """Return a list of all the parameters that are stored in this Source"""
        return [self.island,self.source,self.background,self.local_rms,
                self.ra_str, self.dec_str, self.ra,self.err_ra,self.dec,self.err_dec,
                self.peak_flux, self.err_peak_flux, self.int_flux, self.err_int_flux,
                self.a,self.err_a,self.b,self.err_b,
                self.pa,self.err_pa,self.flags]
    
    def __cmp__(self,other):
        """
        sort order is firstly by island then by source
        both in ascending order
        """
        if self.island>other.island:
            return 1
        elif self.island<other.island:
            return -1
        else:
            if self.source>other.source:
                return 1
            elif self.source<other.source:
                return -1
            else:
                return 0  
    
class GlobalFittingData:
    '''
    The global data used for fitting.
    (should be) Read-only once created.
    Used by island fitting subprocesses.
    wcs parameter used by most functions.
    '''
    dcurve = None
    rmsimg = None
    bkgimg = None
    hdu_header = None
    beam = None
    wcs = None
    
class IslandFittingData:
    '''
    All the data required to fit a single island.
    Instances are pickled and passed to the fitting subprocesses
    
    isle_num = island number (int)
    i = the pixel island (a 2D numpy array of pixel values)
    scalars=(innerclip,outerclip,csigma,max_summits)
    offsets=(xmin,xmax,ymin,ymax)
    '''
    isle_num = 0
    i = None
    scalars = []
    offsets = []

    def __init__(self, isle_num, i, scalars, offsets,doislandflux):
        self.isle_num = isle_num
        self.i = i
        self.scalars = scalars
        self.offsets = offsets
        self.doislandflux = doislandflux

class DummyMP():
    """
    A dummy copy of the mpfit class that just holds the parinfo variables
    This class doesn't do a great deal, but it makes it 'looks' like the mpfit class
    and makes it easy to estimate source parameters when you don't want to do any fitting.
    """

    def __init__(self,parinfo,perror):
        self.params=[]
        for var in parinfo:
            try:
                val=var['value'][0]
            except:
                val=var['value']
            self.params.append(val)
        self.perror=perror
        self.errmsg="There is no error, I just didn't bother fitting anything!"

######################################### FUNCTIONS ###############################

## floodfill functions
def explore(data, rmsimg, status, queue, bounds, cutoffratio, pixel):
    """
    Look for pixels adjacent to <pixel> and add them to the queue
    Don't include pixels that are in the queue or that are below
    the cutoffratio
    
    This version requires an rms image to be present -PJH
    
    Returns: nothing
    """
    (x, y) = pixel
    if x < 0 or y < 0:
        print '\n WTF?! Just found a pixel at coordinate' , pixel
        print 'Something screwy going on, edge masking should have caught this.'
        print '*** Code terminating ***'
        sys.exit()

    if x > 0:
        new = (x - 1, y)
        if not status[new] & flags.QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= flags.QUEUED

    if x < bounds[0]:
        new = (x + 1, y)
        if not status[new] & flags.QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= flags.QUEUED

    if y > 0:
        new = (x, y - 1)
        if not status[new] & flags.QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= flags.QUEUED

    if y < bounds[1]:
        new = (x, y + 1)
        if not status[new] & flags.QUEUED and data[new]/rmsimg[new] >= cutoffratio:
            queue.append(new)
            status[new] |= flags.QUEUED

def flood(data, rmsimg, status, bounds, peak, cutoffratio):
    """
    Start at pixel=peak and return all the pixels that belong to
    the same blob.

    Returns: a list of pixels contiguous to <peak>

    This version requires an rms image - PJH
    """
    if status[peak] & flags.VISITED:
        return []

    blob = []
    queue = [peak]
    status[peak] |= flags.QUEUED

    for pixel in queue:
        if status[pixel] & flags.VISITED:
            continue
    
        status[pixel] |= flags.VISITED

        blob.append(pixel)
        explore(data, rmsimg, status, queue, bounds, cutoffratio, pixel)

    return blob

def gen_flood_wrap(data,rmsimg,innerclip,outerclip=None,expand=True):
    """
    <a generator function>
    Find all the sub islands in data.
    Detect islands with innerclip.
    Report islands with outerclip

    type(data) = Island
    return = [(pixels,xmin,ymin)[,(pixels,xmin,ymin)] ]
    where xmin,ymin is the offset of the subisland
    """
    if outerclip is None:
        outerclip=innerclip
        
    status=np.zeros(data.pixels.shape,dtype=np.uint8)
    # Selecting PEAKED pixels
    logging.debug("InnerClip: {0}".format(innerclip))

    status += np.where(data.pixels/rmsimg>innerclip,flags.PEAKED,0)
    #logging.debug("status: {0}".format(status[1:5,1:5]))
    logging.debug("Peaked pixels: {0}/{1}".format(np.sum(status),len(data.pixels.ravel())))
    # making pixel list
    ax,ay=np.where(data.pixels/rmsimg>innerclip)
    peaks=[(data.pixels[ax[i],ay[i]],ax[i],ay[i]) for i in range(len(ax))]
    if len(peaks)==0:
        logging.debug("There are no pixels above the clipping limit")
        return
    # sorting pixel list
    peaks.sort(reverse=True)
    peaks=map(lambda x:x[1:],peaks)
    logging.debug("Brightest Peak {0}, SNR= {0}/{1}".format(data.pixels[peaks[0]],rmsimg[peaks[0]]))
    logging.debug("Faintest Peak {0}, SNR= {0}/{1}".format(data.pixels[peaks[-1]],rmsimg[peaks[-1]]))
    bounds=(data.pixels.shape[0]-1,data.pixels.shape[1]-1)
    
    # starting image segmentation
    for peak in peaks:
        blob=flood(data.pixels,rmsimg,status,bounds,peak,cutoffratio=outerclip)
        npix=len(blob)
        if npix>=1:#islands with no pixels have length 1
            if expand:
                logging.debug("I don't want to expand")
                logging.error("You said ''expand'' but this is not yet working!")
                sys.exit(1)
            new_isle,xmin,xmax,ymin,ymax=data.list2map(blob)
            if new_isle is not None:
                yield new_isle,xmin,xmax,ymin,ymax
    
##parameter estimates
def estimate_parinfo(data,rmsimg,curve,beam,innerclip,csigma=None,offsets=[0,0]):
    """Estimates the number of sources in an island and returns initial parameters for the fit as well as
    limits on those parameters.

    input:
    data   - np.ndarray of flux values
    rmsimg - np.ndarray of 1sigmas values
    curve  - np.ndarray of curvature values
    beam   - beam object
    innerclip - the inner clipping level for flux data, in sigmas
    csigma - 1sigma value of the curvature map
             None => zero (default)
    offsets - the (x,y) offset of data within it's parent image
              this is required for proper WCS conversions

    returns:
    parinfo object for mpfit
    with all parameters in pixel coords    
    """
    #use a curvature of zero as a default significance cut
    if not csigma:
        csigma=0
    parinfo=[]
    
    #calculate a local beam from the center of the data
    xo,yo= data.shape
    pixbeam = get_pixbeam(beam,offsets[0]+xo/2,offsets[1]+yo/2)
    
    #The position cannot be more than a pixel beam from the initial location
    xo_lim=max(pixbeam.a*np.cos(np.radians(pixbeam.pa)),pixbeam.b*np.sin(np.radians(pixbeam.pa)))
    yo_lim=max(pixbeam.a*np.sin(np.radians(pixbeam.pa)),pixbeam.b*np.cos(np.radians(pixbeam.pa)))
    logging.debug(" - shape {0}".format(data.shape))
    
    if not data.shape == curve.shape:
        logging.error("data and curvature are mismatched")
        logging.error("data:{0} curve:{1}".format(data.shape,curve.shape))
        sys.exit()

    #For small islands we can't do a 6 param fit
    #Don't count the NaN values as part of the island
    non_nan_pix=len(data[np.where(data==data)].ravel())
    if 4<= non_nan_pix and non_nan_pix <= 6:
        logging.debug("FIXED2PSF")
        is_flag=flags.FIXED2PSF
    elif non_nan_pix < 4: 
        logging.debug("FITERRSMALL!")
        is_flag=flags.FITERRSMALL
    else:
        is_flag=0
    logging.debug(" - size {0}".format(len(data.ravel())))

    if min(data.shape)<=2 or (is_flag & flags.FITERRSMALL):
        #1d islands or small islands only get one source
        logging.debug("Tiny summit detected")
        logging.debug("{0}".format(data))
        summits=[ [data,0,data.shape[0],0,data.shape[1]] ]
        #and are constrained to be point sources
        is_flag |= flags.FIXED2PSF
    else:       
        kappa_sigma=Island( np.where( curve<-1*csigma, np.where(data-innerclip*rmsimg>0, data,-1) ,-1) )
        summits=gen_flood_wrap(kappa_sigma,np.ones(kappa_sigma.pixels.shape),0,expand=False)
        
    i=0
    for summit,xmin,xmax,ymin,ymax in summits:
        
        summit_flag = is_flag
        logging.debug("Summit({5}) - shape:{0} x:[{1}-{2}] y:[{3}-{4}]".format(summit.shape,xmin,xmax,ymin,ymax,i))
        #amp = summit[np.where(np.isfinite(summit))].max()
        amp=summit[np.where(summit==summit)].max()#HAXORZ!! stupid NaNs break all my things
        logging.debug(" - max is {0}".format(amp))
        (xpeak,ypeak)=np.where(summit==amp)
        logging.debug(" - peak at {0},{1}".format(xpeak,ypeak))
        xo = xpeak[0]+xmin
        yo = ypeak[0]+ymin
        #allow amp to be 5% or 3sigma higher
        #TODO: the 5% should depend on the beam sampling
        amp_min,amp_max= float(4*rmsimg[xo,yo]), float(amp*1.05+3*rmsimg[xo,yo])
        logging.debug("a_min {0}, a_max {1}".format(amp_min,amp_max))
        
        xo_min,xo_max = max(xmin,xo-xo_lim),min(xmax,xo+xo_lim)
        if xo_min==xo_max: #if we have a 1d summit then allow the position to vary by +/-0.5pix
            xo_min,xo_max=xo_min-0.5,xo_max+0.5
            
        yo_min,yo_max = min(ymin,yo-yo_lim),max(ymax,yo+yo_lim)
        if yo_min==yo_max: #if we have a 1d summit then allow the position to vary by +/-0.5pix
            yo_min,yo_max=yo_min-0.5,yo_max+0.5

        #TODO: The limits on major,minor work well for circular beams or unresolved sources
        #for elliptical beams *and* resolved sources this isn't good and should be redone
        
        xsize=xmax-xmin+1
        ysize=ymax-ymin+1
        
        #initial shape is the pix beam        
        major=pixbeam.a*fwhm2cc
        minor=pixbeam.b*fwhm2cc
        
        #constraints are based on the shape of the island
        major_min,major_max = major*0.8, max((max(xsize,ysize)+1)*math.sqrt(2)*fwhm2cc,major*1.1)
        minor_min,minor_max = minor*0.8, max((max(xsize,ysize)+1)*math.sqrt(2)*fwhm2cc,major*1.1)

        #TODO: update this to fit a psf for things that are "close" to a psf.
        #if the min/max of either major,minor are equal then use a PSF fit
        if minor_min==minor_max or major_min==major_max:
            summit_flag|=flags.FIXED2PSF
        
        pa=pixbeam.pa
        flag=summit_flag
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug(" - var val min max | min max")
            logging.debug(" - amp {0} {1} {2} ".format(amp,amp_min,amp_max))
            logging.debug(" - xo {0} {1} {2} ".format(xo,xo_min,xo_max))
            logging.debug(" - yo {0} {1} {2} ".format(yo,yo_min,yo_max))
            logging.debug(" - major {0} {1} {2} | {3} {4}".format(major,major_min,major_max,major_min/fwhm2cc,major_max/fwhm2cc))
            logging.debug(" - minor {0} {1} {2} | {3} {4}".format(minor,minor_min,minor_max,minor_min/fwhm2cc,minor_max/fwhm2cc))
            logging.debug(" - pa {0} {1} {2}".format(pa,-180,180))
            logging.debug(" - flags {0}".format(flag))
        parinfo.append( {'value':amp,
                         'fixed':False,
                         'parname':'{0}:amp'.format(i),
                         'limits':[amp_min,amp_max],
                         'limited':[True,True]} )
        parinfo.append( {'value':xo,
                         'fixed':False,
                         'parname':'{0}:xo'.format(i),
                         'limits':[xo_min,xo_max],
                         'limited':[True,True]} )
        parinfo.append( {'value':yo,
                         'fixed':False,
                         'parname':'{0}:yo'.format(i),
                         'limits':[yo_min,yo_max],
                         'limited':[True,True]} )
        parinfo.append( {'value':major,
                         'fixed': (flag & flags.FIXED2PSF)>0,
                         'parname':'{0}:major'.format(i),
                         'limits':[major_min,major_max],
                         'limited':[True,True],
                         'flags':flag})
        parinfo.append( {'value':minor,
                         'fixed': (flag & flags.FIXED2PSF)>0,
                         'parname':'{0}:minor'.format(i),
                         'limits':[minor_min,minor_max],
                         'limited':[True,True],
                         'flags':flag} )
        parinfo.append( {'value':pa,
                         'fixed': (flag & flags.FIXED2PSF)>0,
                         'parname':'{0}:pa'.format(i),
                         'limits':[-180,180],
                         'limited':[False,False],
                         'flags':flag} )
        i+=1
    logging.debug("Estimated sources: {0}".format(i))
    return parinfo

def ntwodgaussian(inpars):
    """
    Return an array of values represented by multiple Gaussians as parameterized
    by params = [amp,x0,y0,major,minor,pa]{n}
    x0,y0,major,minor are in pixels
    major/minor are interpreted as being sigmas not FWHMs
    pa is in degrees
    """
    if not len(inpars)%6 ==0:
        logging.error("inpars requires a multiple of 6 parameters")
        logging.error("only {0} parameters supplied".format(len(inpars)))
        sys.exit()
    pars=np.array(inpars).reshape(len(inpars)/6,6)
    amp,xo,yo,major,minor,pa = zip(*pars)
    #transform pa->-pa so that our angles are CW instead of CCW
    st,ct,s2t=zip(*[ (math.sin(np.radians(-p))**2,math.cos(np.radians(-p))**2,math.sin(2*np.radians(-p))) for p in pa])
    a = [ (ct[i]/major[i]**2 + st[i]/minor[i]**2)/2 for i in range(len(amp))]
    bb= [ s2t[i]/4 *(1/minor[i]**2-1/major[i]**2)   for i in range(len(amp))]
    c = [ (st[i]/major[i]**2 + ct[i]/minor[i]**2)/2 for i in range(len(amp))]

    def rfunc(x,y):
        ans=0
        #list comprehension just breaks here, something to do with scope i think
        for i in range(len(amp)):
            ans+= amp[i]*np.exp(-1*(a[i]*(x-xo[i])**2 + 2*bb[i]*(x-xo[i])*(y-yo[i]) + c[i]*(y-yo[i])**2) )
        return ans
    return rfunc

def twodgaussian(params, shape):
    '''
    Build a (single) 2D Gaussian ellipse as parameterised by "params" for a region with "shape"
        params - [amp, xo, yo, cx, cy, pa] where:
                amp - amplitude
                xo  - centre of Gaussian in X
                yo  - centre of Gaussian in Y
                cx  - width of Gaussian in X (sigma or c, not FWHM)
                cy  - width of Gaussian in Y (sigma or c, not FWHM)
                pa  - position angle of Gaussian, aka theta (radians clockwise)
        shape - (y, x) dimensions of region
    Returns a 2D numpy array with shape="shape"
    
    Actually just calls ntwodgaussian!!
    '''
    assert(len(shape) == 2)
    return ntwodgaussian(params)(*np.indices(shape))

def multi_gauss(data,rmsimg,parinfo):
    """
    Fit multiple gaussian components to data using the information provided by parinfo.
    data may contain 'flagged' or 'masked' data with the value of np.NaN
    input: data - pixel information
           rmsimg - image containing 1sigma values
           parinfo - initial parameters for mpfit
    return: mpfit object, parameter info
    """
    
    data=np.array(data)
    mask=np.where(data==data) #the indices of the *non* NaN values in data
    
    def model(p):
        """Return a map with a number of gaussians determined by the input parameters."""
        return ntwodgaussian(p)(*mask)
        
    def erfunc(p,fjac=None):
        """The difference between the model and the data"""
        return [0,np.ravel( (model(p)-data[mask] )/rmsimg[mask])]
    
    mp=mpfit(erfunc,parinfo=parinfo,quiet=True)

    return mp,parinfo

#load and save external files
def load_aux_image(image,auxfile):
    """
    Load a fits file (bkg/rms/curve) and make sure that
    it is the same shape as the main image.
    image = main image object
    auxfile = filename of auxiliary file
    """
    auximg=FitsImage(auxfile).get_pixels()
    if auximg.shape != image.get_pixels().shape:
        logging.error("file {0} is not the same size as the image map".format(auxfile))
        logging.error("{0}= {1}, image = {2}".format(auxfile,auximg.shape, image.get_pixels().shape))
        sys.exit()
    return auximg
        
def load_bkg_rms_image(image,bkgfile,rmsfile):
    """
    Load the background and rms images.
    Deprecation iminent:
      use load_aux_image on individual images
    """
    bkgimg = load_aux_image(image,bkgfile)
    rmsimg = load_aux_image(image,rmsfile)
    return bkgimg,rmsimg

def load_catalog(filename,fmt='csv'):
    '''
    load a catalog and extract the source positions
    acceptable formats are:
    csv - ra, dec as decimal degrees in FIRST two columns
    cat - format created by Aegean
    returns [(ra,dec),...]
    '''
    if fmt=='csv':
        lines=[a.strip().split(',') for a in open(filename,'r').readlines() if not a.startswith('#') ]
        catalog=[ (float(a[0]),float(a[1])) for a in lines]      
    elif fmt=='cat':
        lines = [a.strip().split() for a in open(filename,'r').readlines() if not a.startswith('#') ]
        catalog = [ (float(a[5]),float(a[7])) for a in lines]
    else:
        logging.error("Catalog file {0} not loaded".format(filename))
        catalog=None
        
    return catalog

def save_catalog(filename,catalog):
    '''
    input:
        filename - name of file to write, format determined by extension
        catalog - a list of sources (OutputSources or SimpleSources)
    returns:
        nothing
    '''
    #.ann and .reg are handled by me
    extension=os.path.basename(filename).split('.')[-1]
    if extension in ['ann','reg']:
        writeAnn(filename,catalog,extension)
    else:    #the remaining extensions are handeled by atpy
        try:
            import atpy
        except ImportError as e:
            logging.error("In order to save in formats other than Aegean default you need to be able to import atpy")
            logging.error(e.message)
            logging.warning("File not saved")
            return
        
        t=atpy.Table()
        t.table_name="Aegean Source Catalog"
        t.add_comment="Created by Aegean {0}".format(version)
        
        columns = catalog[0].__dict__.keys()
        
        for c in columns:
            data = [getattr(a,c) for a in catalog]
            t.add_column(c,data,description=a.meta['name'][c],unit=a.meta['unit'][c],dtype=a.meta['type'][c])
        if os.path.exists(filename):
            os.popen('rm {0}'.format(filename))
        t.write(filename)
    logging.info("wrote {0}".format(filename))
    return

def writeAnn(filename,catalog,fmt):
    """
    Write an annotation file that can be read by Kvis (.ann) or DS9 (.reg).
    Uses ra/dec from catalog.
    Draws ellipses if bmaj/bmin/pa are in catalog 
    Draws 30" circles otherwise
    Input:
        filename - file to write to
        catalog - a list of OutputSource or SimpleSource
        fmt - [.ann|.reg] format to use
    """
    out=open(filename,'w')
    ras = [a.ra for a in catalog]
    decs= [a.dec for a in catalog]
    if not hasattr(catalog[0],'a'): #a being the variable that I used for bmaj.
        bmajs=[30/3600.0 for a in catalog]
        bmins=bmaj
        pas = [0 for a in catalog]
    else:
        bmajs = [a.a/3600.0 for a in catalog]
        bmins = [a.b/3600.0 for a in catalog]
        pas = [a.pa for a in catalog]
    
    
    if fmt=='ann':
        print >>out,'PA SKY'
        formatter="ellipse {0} {1} {2} {3} {4}"
    elif fmt=='reg':
        print >>out,"fk5"
        formatter='ellipse {0} {1} {2}d {3}d {4}d'
        
    for ra,dec,bmaj,bmin,pa in zip(ras,decs,bmajs,bmins,pas):
        print >>out,formatter.format(ra,dec,bmaj,bmin,pa)
    out.close()
    return
        
#image manipulation
def make_bkg_rms_image(data,beam,mesh_size=20,forced_rms=None):
    """
    Calculate an rms image and a bkg image
    
    inputs:
    data - np.ndarray of flux values
    beam - beam object
    mesh_size - number of beams per box
                default = 20
    forced_rms - the rms of the image
                None => calculate the rms and bkg levels (default)
                <float> => assume zero background and constant rms

    return:
    bkgimg - np.ndarray of background offsets
    rmsimg - np.ndarray of 1 sigma levels
    """
    if forced_rms:
        return np.zeros(data.shape),np.ones(data.shape)*forced_rms
    
    img_y,img_x = data.shape
    xcen=int(img_x/2)
    ycen=int(img_y/2)

    #calculate a local beam from the center of the data
    pixbeam=get_pixbeam(beam,xcen,ycen)
    
    width_x = mesh_size*max(abs(math.cos(np.radians(pixbeam.pa))*pixbeam.b),
                            abs(math.sin(np.radians(pixbeam.pa))*pixbeam.a) )
    width_x = int(width_x)
    width_y = mesh_size*max(abs(math.sin(np.radians(pixbeam.pa))*pixbeam.b),
                            abs(math.cos(np.radians(pixbeam.pa))*pixbeam.a) )
    width_y = int(width_y)
    
    rmsimg = np.zeros(data.shape)
    bkgimg = np.zeros(data.shape)
    logging.debug("image size x,y:{0},{1}".format(img_x,img_y))
    logging.debug("beam: {0}".format(beam))
    logging.debug("mesh width (pix) x,y: {0},{1}".format(width_x,width_y))

    #box centered at image center then tilling outwards
    xstart=(xcen-width_x/2)%width_x #the starting point of the first "full" box
    ystart=(ycen-width_y/2)%width_y
    
    xend=img_x - (img_x-xstart)%width_x #the end point of the last "full" box
    yend=img_y - (img_y-ystart)%width_y
      
    xmins=[0]
    xmins.extend(range(xstart,xend,width_x))
    xmins.append(xend)
    
    xmaxs=[xstart]
    xmaxs.extend(range(xstart+width_x,xend+1,width_x))
    xmaxs.append(img_x)
    
    ymins=[0]
    ymins.extend(range(ystart,yend,width_y))
    ymins.append(yend)
    
    ymaxs=[ystart]
    ymaxs.extend(range(ystart+width_y,yend+1,width_y))
    ymaxs.append(img_y)

    #if the image is smaller than our ideal mesh size, just use the whole image instead
    if width_x >=img_x:
        xmins=[0]
        xmaxs=[img_x]
    if width_y >=img_y:
        ymins=[0]
        ymaxs=[img_y]

    for xmin,xmax in zip(xmins,xmaxs):
        for ymin,ymax in zip(ymins,ymaxs):
            bkg, rms = estimate_background(data[ymin:ymax,xmin:xmax])
            rmsimg[ymin:ymax,xmin:xmax] = rms
            bkgimg[ymin:ymax,xmin:xmax] = bkg
  
    return bkgimg,rmsimg

def estimate_background(data):
    '''
    Estimate the background noise mean and RMS.
    The mean is estimated as the median of data.
    The RMS is estimated as the IQR of data / 1.34896.
    Returns (bkg, rms).
    Returns (NaN, NaN) if data contains fewer than 4 values.
    '''
    pixels = np.extract(data==data, data).ravel()
    if len(pixels) < 4:
        return np.NaN, np.NaN
    pixels.sort()
    p25 = pixels[pixels.size/4]
    p50 = pixels[pixels.size/2]
    p75 = pixels[pixels.size/4*3]
    iqr = p75 - p25
    return p50, iqr / 1.34896
    
def curvature(data,aspect=None):
    """
    Use a Lapacian kernal to calculate the curvature map.
    input:
        data - the image data
        aspect - the ratio of pixel size (x/y)
                NOT TESTED!
    """
    if not aspect:
        kern=np.array( [[1,1,1],[1,-8,1],[1,1,1]])
    else:
        logging.warn("Aspect != has not been tested.")
        #TODO: test that this actually works as intended
        a = 1.0/aspect
        b = 1.0/math.sqrt(1+aspect**2)
        c = -2.0*(1+a+2*b)
        kern = 0.25*np.array( [[b,a,b],[1,c,1],[b,a,b]])
    return ndi.convolve(data,kern)

##Nifty helpers
def within(x,xm,xx):
    """Enforce xm<= x <=xx"""
    return min(max(x,xm),xx)

def fix_shape(source):
    """
    Ensure that a>=b
    adjust as required
    """
    if source.a<source.b:
        source.a,source.b = source.b, source.a
        source.err_a, source.err_b = source.err_b, source.err_a
        source.pa +=90
    return

def pa_limit(pa):
    """
    Position angle is periodic with period 180\deg
    Constrain pa such that -90<pa<=90
    """
    while pa<=-90:
        pa+=180
    while pa>90:
        pa-=180
    return pa

#WCS helper functions

def pix2sky(pixel):
    """
    Take pixel=(x,y) coords
    convert to pos=(ra,dec) coords
    """
    x,y=pixel
    #wcs and pyfits have oposite ideas of x/y
    skybox = global_data.wcs.wcs_pix2sky([[y,x]],1)
    return [float(skybox[0][0]), float(skybox[0][1])]

def sky2pix(pos):
    """
    Take pos = (ra,dec) coords
    convert to pixel = (x,y) coords
    """
    pixel = global_data.wcs.wcs_sky2pix([pos],1)
    #wcs and pyfits have oposite ideas of x/y
    return [pixel[0][1],pixel[0][0]]

def sky2pix_vec(pos,r,pa):
    """Convert a vector from sky to pixel corrds
    vector is calculated at an origin pos=(ra,dec)
    and has a magnitude (r) [in degrees]
    and an angle (pa) [in degrees]
    input:
        pos - (ra,dec) of vector origin
        r - magnitude in degrees
        pa - angle in degrees
    return:
    x,y - corresponding to position ra,dec
    r,theta - magnitude (piexls) and angle (degrees) of the origional vector
    """
    ra,dec= pos
    x,y = sky2pix(pos)
    a = translate(ra,dec,r,pa)
    #[ra +r*np.sin(np.radians(pa))*np.cos(np.radians(dec)),
    #     dec+r*np.cos(np.radians(pa))]
    locations=sky2pix(a)
    x_off,y_off = locations
    a=np.sqrt((x-x_off)**2 + (y-y_off)**2)
    theta=np.degrees(np.arctan2((y_off-y),(x_off-x)))
    return (x,y,a,theta)

def pix2sky_vec(pixel,r,theta):
    """
    Convert a vector from pixel to sky coords
    vector is calculated at an origin pixel=(x,y)
    and has a magnitude (r) [in pixels]
    and an angle (theta) [in degrees]
    input:
        pixel - (x,y) of origin
        r - magnitude in pixels
        theta - in degrees
    return:
    ra,dec - corresponding to pixels x,y
    r,pa - magnitude and angle (degrees) of the origional vector, as measured on the sky
    """
    ra1,dec1 = pix2sky(pixel)
    x,y=pixel
    a = [ x+r*np.cos(np.radians(theta)),
          y+r*np.sin(np.radians(theta))]
    locations = pix2sky(a)
    ra2,dec2 = locations
    a = gcd(ra1,dec1,ra2,dec2)
    pa = bear(ra1,dec1,ra2,dec2)
    return (ra1,dec1,a,pa)

def get_pixbeam(beam,x,y):
    """
    Calculate a beam with parameters in pixel coordinates
    at the given image location (x,y)
    Input:
        beam - a Beam object
        x,y - the pixel coordinates at which to comput the beam   
    Returns:
        a beam where beam.a, beam.b are in pixels
        and beam.pa is in degrees
    """
    #calculate a local beam from the center of the data
    ra,dec = pix2sky([x,y])
    major,pa =sky2pix_vec([ra,dec],beam.a,beam.pa)[2:4]
    minor = sky2pix_vec([ra,dec],beam.b,beam.pa + 90)[2]
    return Beam(major,minor,pa)

def sky_sep(pix1,pix2):
    """
    calculate the sky separation between two pixels
    Input:
        pix1 = [x1,y1]
        pix2 = [x2,y2]
    Returns:
        sep = separation in degrees
    """
    pos1 = pix2sky(pix1)
    pos2 = pix2sky(pix2)
    sep = gcd(pos1[0],pos1[1],pos2[0],pos2[1])
    return sep
######################################### THE MAIN DRIVING FUNCTIONS ###############

#source finding and fitting
def fit_island(island_data):
    """
    Take an Island and do all the parameter estimation and fitting.
    island_data - an IslandFittingData object
    Return a list of sources that are within the island.
    None = no sources found in the island.
    """
    global global_data

    # global data
    hdu_header = global_data.hdu_header
    dcurve = global_data.dcurve
    rmsimg = global_data.rmsimg
    bkgimg = global_data.bkgimg
    beam = global_data.beam
    wcs = global_data.wcs
    
    # island data
    isle_num = island_data.isle_num
    idata = island_data.i        
    innerclip,outerclip,csigma,max_summits=island_data.scalars
    xmin,xmax,ymin,ymax=island_data.offsets
    doislandflux = island_data.doislandflux

    isle=Island(idata)
    icurve = dcurve[xmin:xmax+1,ymin:ymax+1]
    rms=rmsimg[xmin:xmax+1,ymin:ymax+1]
    bkg=bkgimg[xmin:xmax+1,ymin:ymax+1]

    pixbeam = get_pixbeam(beam,(xmin+xmax)/2,(ymin+ymax)/2)

    logging.debug("=====")
    logging.debug("Island ({0})".format(isle_num))

    parinfo= estimate_parinfo(isle.pixels,rms,icurve,beam,innerclip,csigma=csigma,offsets=[xmin,ymin])

    logging.debug("Rms is {0}".format(np.shape(rms)) )
    logging.debug("Isle is {0}".format(np.shape(isle.pixels)) )
    logging.debug(" of which {0} are masked".format(sum(np.isnan(isle.pixels).ravel()*1)))

    # skip islands with too many summits (Gaussians)
    num_summits = len(parinfo) / 6 # there are 6 params per Guassian
    logging.debug("max_summits, num_summits={0},{1}".format(max_summits,num_summits))

    # Islands may have no summits if the curvature is not steep enough.
    if num_summits < 1:
        logging.debug("Island {0} has no summits!".format(isle_num))
        return []

    #extract a flag for the island
    is_flag=0
    for src in parinfo:
        if src['parname'].split(":")[-1] in ['minor','major','pa']:
            if src['flags'] & flags.FITERRSMALL:
                is_flag=src['flags']
                break
    if (max_summits is not None) and (num_summits > max_summits):
        logging.info("Island has too many summits ({0}), not fitting anything".format(num_summits))
        #set all the flags to be NOTFIT
        for src in parinfo:
            if src['parname'].split(":")[-1] in ['minor','major','pa']:
                src['flags']|=flags.NOTFIT
        mp=DummyMP(parinfo=parinfo,perror=None)
        info=parinfo
    elif is_flag & flags.FITERRSMALL:
        logging.debug("Island is too small for a fit, not fitting anything")
        #set all the flags to be NOTFIT
        for src in parinfo:
            if src['parname'].split(":")[-1] in ['minor','major','pa']:
                src['flags']|=flags.NOTFIT
        mp=DummyMP(parinfo=parinfo,perror=None)
        info=parinfo
    else:
        mp,info=multi_gauss(isle.pixels,rms,parinfo)

    logging.debug("Source 0 pa={0} [pixel coords]".format(mp.params[5]))
    
    params=mp.params
    #report the source parameters
    err=False
    sources=[]
    parlen=len(params)
    
    #fix_shape(mp)
    par_matrix = np.asarray(params,dtype=np.float64) #float32's give string conversion errors.
    par_matrix = par_matrix.reshape(parlen/6,6)
        
    #if there was a fitting error create an mp.perror matrix full of zeros
    if mp.perror is None:
        mp.perror = [0 for a in mp.params]
        err=True
        logging.debug("FitError: {0}".format(mp.errmsg))
        logging.debug("info:")
        for i in info:
            logging.debug("{0}".format(i))
            
    #anything that has an error of zero should be converted to -1
    for k,val in enumerate(mp.perror):
        if val==0.0:
            mp.perror[k]=-1

    err_matrix = np.asarray(mp.perror).reshape(parlen/6,6)
    components= parlen/6
    for j,((amp,xo,yo,major,minor,theta),(amp_err,xo_err,yo_err,major_err,minor_err,theta_err)) in enumerate(zip(par_matrix,err_matrix)):
        source = OutputSource()
        source.island = isle_num
        source.source = j
        
        #record fitting and error flags
        src_flags=0
        if err:
            src_flags|=flags.FITERR
            
        #read the flag information from the 'pa'
        src_flags|= info[j*6+5]['flags']

        #params = [amp,x0,y0,major,minor,pa]{n}
        #pixel pos within island + 
        # island offset within region +
        # region offset within image +
        # 1 for luck
        # (pyfits->fits conversion = luck)
        x_pix=xo + xmin + 1
        y_pix=yo + ymin + 1

        (source.ra,source.dec,source.a,source.pa) = pix2sky_vec((x_pix,y_pix),major*cc2fwhm,theta)
        source.ra_str= dec2hms(source.ra)
        source.dec_str= dec2dms(source.dec)
        logging.debug("Source {0} Extracted pa={1}deg [pixel] -> {2}deg [sky]".format(j,theta,source.pa))
        
        #calculate minor axis and convert a/b to arcsec
        source.a *= 3600 #arcseconds
        source.b = pix2sky_vec((x_pix,y_pix),minor*cc2fwhm,theta+90)[2]*3600 #arcseconds

        #calculate ra,dec errors from the pixel error
        #limit the errors to be the width of the island
        x_err_pix=x_pix + within(xo_err,-1,isle.pixels.shape[0])
        y_err_pix=y_pix + within(yo_err,-1,isle.pixels.shape[1])
        err_coords = pix2sky([x_err_pix, y_err_pix])
        source.err_ra = abs(source.ra - err_coords[0])   if xo_err>0 else -1
        source.err_dec = abs(source.dec - err_coords[1]) if yo_err>0 else -1
        source.err_a = abs(source.a - pix2sky_vec((x_pix,y_pix),(major+major_err)*cc2fwhm,theta)[2]*3600)    if major_err>0 else -1
        source.err_b = abs(source.b - pix2sky_vec((x_pix,y_pix),(minor+minor_err)*cc2fwhm,theta+90)[2]*3600) if minor_err>0 else -1
        source.err_pa= abs(source.pa - pix2sky_vec((x_pix,y_pix),major*cc2fwhm,theta+theta_err)[3])          if theta_err>0 else -1
        #ensure a>=b
        fix_shape(source)
        #fix the pa to be between -90<pa<=90
        source.pa = pa_limit(source.pa)
        if source.err_pa>0:
            # pa-err_pa = 180degrees is the same as 0degrees so change it
            source.err_pa = abs(pa_limit(source.err_pa))
        
        
        # flux values
        #the background is taken from background map
        # Clamp the pixel location to the edge of the background map (see Trac #51)
        y = max(min(int(round(y_pix-ymin)), bkg.shape[1]-1),0)
        x = max(min(int(round(x_pix-xmin)), bkg.shape[0]-1),0)
        #FIXME check that these x,y are correct. maybe = y-1,x-1
        source.background=bkg[x,y]
        source.local_rms=rms[x,y]
        source.peak_flux = amp
        source.err_peak_flux = amp_err
        
        source.flags = src_flags
        
        pixbeam=get_pixbeam(beam,x_pix,y_pix)
        #integrated flux is calculated not fit or measured
        source.int_flux=source.peak_flux*major*minor*cc2fwhm**2/(pixbeam.a*pixbeam.b)
        #The error is never -1, but may be zero.
        source.err_int_flux = source.int_flux*math.sqrt( (max(source.err_peak_flux,0)/source.peak_flux)**2
                                                        +(max(source.err_a,0)/source.a)**2
                                                        +(max(source.err_b,0)/source.b)**2)

        #fiddle all the errors to be larger by sqrt(npix)
        rt_npix=np.sqrt(sum(np.isfinite(isle.pixels).ravel()*1)/components)
        if source.err_ra>0:
            source.err_ra *=rt_npix
        if source.err_dec>0:
            source.err_dec *=rt_npix
        if source.err_peak_flux>0:
            source.err_peak_flux *=rt_npix
        if source.err_int_flux>0:
            source.err_int_flux *=rt_npix
        if source.err_a>0:
            source.err_a *=rt_npix        
        if source.err_b>0:
            source.err_b *=rt_npix        
        if source.err_pa>0:
            source.err_pa *=rt_npix       

        sources.append(source)
        logging.debug(source)

    #calculate the integrated island flux if required
    if island_data.doislandflux:
        logging.debug("Integrated flux for island {0}".format(isle_num))
        kappa_sigma=np.where(idata-innerclip*rms>0, idata,0)
        logging.debug("- island shape is {0}".format(kappa_sigma.shape))

        source = OutputSource()
        source.island=isle_num
        source.source = -1
        source.flags=0
        source.peak_flux = kappa_sigma.max()
        source.err_peak_flux = -1
        #positions and background
        positions = np.where(kappa_sigma == kappa_sigma.max())
        xy=positions[0][0] +xmin, positions[1][0]+ymin
        radec = pix2sky(xy)
        source.ra = radec[0]
        source.dec= radec[1]
        source.err_ra=-1
        source.err_dec=-1
        source.ra_str = dec2hms(source.ra)
        source.dec_str = dec2dms(source.dec)
        source.background = bkg[positions[0][0],positions[1][0]]
        source.local_rms = rms[positions[0][0],positions[1][0]]
        #unused info
        source.a=0
        source.err_a=-1
        source.b=0
        source.err_b=-1
        source.pa=0
        source.err_pa=-1

        logging.debug("- peak flux {0}".format(source.peak_flux))
        logging.debug("- peak position {0}, {1}".format(source.ra_str,source.dec_str))

        #integrated flux
        beam_volume = 2*np.pi*pixbeam.a*pixbeam.b/cc2fwhm**2
        isize = len(np.where(kappa_sigma>0)[0]) #number of non zero pixels
        logging.debug("- pixels used {0}".format(isize))
        source.int_flux = sum(sum(kappa_sigma)) #total flux Jy/beam
        logging.debug("- sum of pixles {0}".format(source.int_flux))
        source.int_flux /= beam_volume
        logging.debug("- pixbeam {0},{1}".format(pixbeam.a,pixbeam.b))
        logging.debug("- raw integrated flux {0}".format(source.int_flux))
        eta = erf(np.sqrt(-1*np.log(source.local_rms*outerclip/source.peak_flux)))**2
        source.int_flux = source.int_flux / eta**2
        logging.debug("- eta {0}".format(eta))
        logging.debug("- corrected integrated flux {0}".format(source.int_flux))
        source.err_int_flux =-1
        sources.append(source)
    return sources

def fit_islands(islands):
    '''
    Execute fitting on a list of islands.
      islands - a list of IslandFittingData objects
    Returns a list of OutputSources
    '''
    logging.debug("Fitting group of {0} islands".format(len(islands)))
    sources = []
    for island in islands:
        res = fit_island(island)
        sources.extend(res)
    return sources
    
def find_sources_in_image(filename, hdu_index=0, outfile=None,rms=None, max_summits=None, csigma=None,
                          innerclip=5, outerclip=4, cores=None, rmsin=None, bkgin=None, beam=None, doislandflux=False):
    """
    Run the Aegean source finder.
    Inputs:
    filename    - the name of the input file (FITS only)
    hdu_index   - the index of the FITS HDU (extension)
                   Default = 0
    outfile     - print the resulting catalogue to this file
                   Default = None = don't print to a file
    rms         - use this rms for the entire image (will also assume that background is 0)
                   default = None = calculate rms and background values
    max_summits - ignore any islands with more summits than this
                   Default = None = no limit
    csigma      - use this as the clipping limit for the curvature map
                   Default = None = calculate the curvature rms and use 1sigma
    innerclip   - the seeding clip, in sigmas, for seeding islands of pixels
                   Default = 5
    outerclip   - the flood clip in sigmas, used for flooding islands
                   Default = 4
    cores       - number of CPU cores to use. None means all cores.
    rmsin       - filename of an rms image that will be used instead of
                   the internally calculated one
    bkgin       - filename of a background image that will be used instead of
                    the internally calculated one
    beam        - (major,minor,pa) (all degrees) of the synthesised beam to be use
                   overides whatever is given in the fitsheader.
    doislandflux- if true, an integrated flux will be returned for each island in addition to 
                    the individual component entries.
    Return:
    a list of OutputSource objects
    """
    if cores is not None:
        assert(cores >= 1), "cores must be one or more"
        
    global global_data
    
    img = FitsImage(filename, hdu_index=hdu_index,beam=beam)
    hdu_header = img.get_hdu_header()
    beam=img.beam    
    data = Island(img.get_pixels())
    dcurve=curvature(img.get_pixels())
    
    # Save global data for use by fitting subprocesses    
    global_data = GlobalFittingData()
    global_data.beam = beam
    global_data.dcurve = dcurve
    global_data.hdu_header = hdu_header
    try:
        global_data.wcs=pywcs.WCS(hdu_header, naxis=2)
    except:
        global_data.wcs=pywcs.WCS(str(hdu_header),naxis=2)
        
    #if either of rms or bkg images are not supplied then caclucate them both
    if not (rmsin and bkgin):
        logging.info("Calculating background and rms data")
        bkgimg,rmsimg = make_bkg_rms_image(data.pixels,beam,mesh_size=20,forced_rms=rms)

    #replace the calculated images with input versions, if the user has supplied them.
    if bkgin:
        logging.info("loading background data from file {0}".format(bkgin))
        bkgimg = load_aux_image(img,bkgin)        
    if rmsin:
        logging.info("Loading rms data from file {0}".format(rmsin))
        rmsimg = load_aux_image(img,rmsin)
    
    #the curvature images is always calculated
    if csigma is None:
        logging.info("Calculating curvature data")
        cbkg, csigma = estimate_background(dcurve)

    #add the background and rms to the global data
    global_data.bkgimg = bkgimg
    global_data.rmsimg = rmsimg
    
 
    logging.info("beam = {0:5.2f}'' x {1:5.2f}'' at {2:5.2f}deg".format(beam.a*3600,beam.b*3600,beam.pa))
    logging.info("csigma={0}".format(csigma))
    logging.info("seedclip={0}".format(innerclip))
    logging.info("floodclip={0}".format(outerclip))
    
    isle_num=0

    if cores == 1: #single-threaded, no parallel processing
        queue = []
    else:
        if cores is None:
            cores=multiprocessing.cpu_count()
            logging.info("Found {0} cores".format(cores))
        else:
            logging.info("Using {0} subprocesses".format(cores))
        try:
            queue = pprocess.Queue(limit=cores,reuse=1)
            fit_parallel = queue.manage(pprocess.MakeReusable(fit_islands))
        except AttributeError, e:
            if 'poll' in e.message:
                logging.warn("Your O/S doesn't support select.poll(): Reverting to cores=1")
                cores=1
                queue=[]
            else:
                raise e
    
    sources = []

    if outfile:
        print >>outfile,header.format(version,filename)
        print >>outfile,OutputSource.header
    island_group = []
    group_size = 20
    for i,xmin,xmax,ymin,ymax in gen_flood_wrap(data,rmsimg,innerclip,outerclip,expand=False):
        if len(i)<=1:
            #empty islands have length 1
            continue 
        isle_num+=1
        scalars=(innerclip,outerclip,csigma,max_summits)
        offsets=(xmin,xmax,ymin,ymax)
        island_data = IslandFittingData(isle_num, i, scalars, offsets, doislandflux)
        # If cores==1 run fitting in main process. Otherwise build up groups of islands
        # and submit to queue for subprocesses. Passing a group of islands is more
        # efficient than passing single islands to the subprocesses.
        if cores == 1:
            res = fit_island(island_data)
            queue.append(res)
        else:
            island_group.append(island_data)
            # If the island group is full queue it for the subprocesses to fit
            if len(island_group) >= group_size:
                fit_parallel(island_group)
                island_group = []
    
    # The last partially-filled island group also needs to be queued for fitting
    if len(island_group) > 0:
        fit_parallel(island_group) 
        
    for src in queue:
        if src:# ignore src==None
            sources.extend(src)
    if outfile:
        for source in sorted(sources):
            outfile.write(str(source))
            outfile.write("\n")

    return sources

#just flux measuring
def force_measure_flux(img,bkgimg,radec,rmsimg=None):
    '''
    Measure the flux of a point source at each of the specified locations
    Not fitting is done, just forced measurements
    input:
        img - the image data
        bkgimg - the background image
        radec - the locations at which to measure fluxes
        rmsimg - if given, the rms at each location will also be taken
                 from this image and reported
    returns:
    [(flux,err),...]
    '''
    catalog = []
    
    for ra,dec in radec:
        #find the right pixels from the ra/dec
        source_x, source_y = sky2pix([ra,dec])
        x = int(round(source_x))
        y = int(round(source_y))
        if not 0<=x<img.x or not 0<=y<img.y:
            logging.warn("Source at {0} {1} is outside of image bounds".format(ra,dec))
            logging.warn("No measurements made")
            continue


        #make a pixbeam at this location
        pixbeam = get_pixbeam(global_data.beam,source_x,source_y)
        #determine the x and y extent of the beam
        xwidth = 2*pixbeam.a*pixbeam.b
        xwidth/= np.hypot(pixbeam.b*np.sin(np.radians(pixbeam.pa)),pixbeam.a*np.cos(np.radians(pixbeam.pa)))
        ywidth = 2*pixbeam.a*pixbeam.b
        ywidth/= np.hypot(pixbeam.b*np.cos(np.radians(pixbeam.pa)), pixbeam.a*np.sin(np.radians(pixbeam.pa)))
        #round to an int and add 1
        ywidth=int(round(ywidth))+1
        xwidth=int(round(xwidth))+1

        #cut out an image of this size
        xmin = max(0, x - xwidth/2)
        ymin = max(0, y - ywidth/2)
        xmax = min(img.x, x + xwidth/2 + 1)
        ymax = min(img.y, y + ywidth/2 + 1)
        #data = img.get_pixels()[ymin:ymax, xmin:xmax]
        data = img.get_pixels()[xmin:xmax, ymin:ymax]
        
        # Make a Gaussian equal to the beam with amplitude 1.0 at the position of the source
        # in terms of the pixel region.
        amp = 1.0
        xo = source_x - xmin
        yo = source_y - ymin
        params = [amp, xo, yo, pixbeam.a*fwhm2cc, pixbeam.b*fwhm2cc, pixbeam.pa]
        gaussian_data  = ntwodgaussian(params)(*np.indices(data.shape))
        #gaussian_data = twodgaussian(params, data.shape)
        
        # Calculate the "best fit" amplitude as the average of the implied amplitude
        # for each pixel. Error is stddev.
        # Only use pixels within the FWHM, ie value>=0.5. Set the others to NaN
        ratios = np.where(gaussian_data>=0.5, data/gaussian_data, np.nan)
        ratios_no_nans = np.extract(np.isfinite(ratios), ratios) # get rid of NaNs
        flux = np.average(ratios_no_nans)
        error = np.std(ratios_no_nans)
        source = SimpleSource()
        source.ra=ra
        source.dec=dec
        source.peak_flux=flux
        source.err_peak_flux=error
        source.background=bkgimg[x,y]
        
        if rmsimg:
            source.local_rms =rmsimg[x,y]
        catalog.append(source)
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug("Measured source {0}".format(source))
            logging.debug("  used area = [{0}:{1},{2}:{3}]".format(xmin,xmax, ymin,ymax))
            logging.debug("  xo,yo = {0},{1}".format(xo,yo))
            logging.debug("  params = {0}".format(params))
            logging.debug("  flux at [xmin+xo,ymin+yo]' = {0} Jy".format(data[int(xo),int(yo)]))
    return catalog

def measure_catalog_fluxes(filename, catfile, hdu_index=0,outfile=None, bkgin=None, beam=None):
    '''
    Measure the flux at a given set of locations, assuming point sources.
    
    Input:
        filename - fits image file name to be read
        catfile - a catalog of source positions (ra,dec)
        hdu_index - if fits file has more than one hdu, it can be specified here
        outfile - the output file to write to
        bkgin - a background image filename
        beam - beam parameters to overide those given in fits header
        
    '''
    #load fitsfile
    img = FitsImage(filename, hdu_index=hdu_index,beam=beam)
    #hdu_header = img.get_hdu_header()
    beam=img.beam    
    data = Island(img.get_pixels())
    if bkgin:
        logging.info("Loading background data from file {0}".format(bkgin))
        bkgimg = load_aux_image(img,bkgin)
    else:
        logging.info("Calculating background data")
        bkgimg = make_bkg_rms_image(data.pixels,beam,mesh_size=20,forced_rms=1)[0]
    
    #load catalog
    if catfile.split('.')[-1]=='cat':
        logging.debug("Using Aegean catalog file format")
        fmt='cat'
    elif catfile.split('.')[-1]=='csv':
        logging.debug("Using csv file format")
        fmt='csv'
    else:
        logging.error("Unkonwn file format for {0}".format(catfile))
        sys.exit()
        
    radec= load_catalog(catfile,fmt=fmt)
    
    global global_data
    global_data = GlobalFittingData()
    global_data.beam = img.beam
    global_data.hdu_header = img.get_hdu_header()
    try:
        global_data.wcs=pywcs.WCS(global_data.hdu_header, naxis=2)
    except:
        global_data.wcs=pywcs.WCS(str(global_data.hdu_header),naxis=2)
    
    #measure fluxes
    sources = force_measure_flux(img,bkgimg,radec)

    #write output
    print >>outfile, header.format(version,filename)
    print >>outfile, SimpleSource.header
    for source in sources:
        print >>outfile, str(source)
    return sources

#secondary capabilities
def save_background_files(image_filename, hdu_index=0):
    '''
    Generate and save the background and RMS maps as FITS files.
    They are saved in the current directly as aegean-background.fits and aegean-rms.fits.
    '''
    global global_data
    
    logging.info("Saving background / RMS maps")
    img = FitsImage(image_filename, hdu_index=hdu_index)
    data = img.get_pixels()
    beam=img.beam
    hdu_header = img.get_hdu_header()
    dcurve= curvature(data) 
    # Save global data for use by rms/bkg calcs     
    global_data = GlobalFittingData()
    global_data.beam = beam
    global_data.dcurve = dcurve
    global_data.hdu_header = hdu_header
    try:
        global_data.wcs=pywcs.WCS(hdu_header, naxis=2)
    except:
        global_data.wcs=pywcs.WCS(str(hdu_header),naxis=2)
    
    bkgimg,rmsimg = make_bkg_rms_image(data,beam,mesh_size=20)
    
    # Generate the new FITS files by copying the existing HDU and assigning new data.
    # This gives the new files the same WCS projection and other header fields. 
    new_hdu = img.hdu
    # Set the ORIGIN to indicate Aegean made this file
    new_hdu.header.update("ORIGIN", "Aegean {0}".format(version))
    new_hdu.data = bkgimg
    new_hdu.writeto("aegean-background.fits", clobber=True)
    new_hdu.data = rmsimg
    new_hdu.writeto("aegean-rms.fits", clobber=True)
    new_hdu.data = dcurve
    new_hdu.writeto("aegean-curvature.fits",clobber=True)
    logging.info("Saved aegean-background.fits, aegean-rms.fits and aegean-curvature.fits")

#command line version of this program runs from here.    
if __name__=="__main__":
    usage="usage: %prog [options] FileName.fits"
    parser = OptionParser(usage=usage)
    parser.add_option("--cores", dest="cores", type="int",
                      help="Number of CPU cores to use for processing [default: all cores]")
    parser.add_option("--debug", dest="debug", action="store_true",
                      help="Enable debug log output")
    parser.add_option("--hdu", dest="hdu_index", type="int",
                      help="HDU index (0-based) for cubes with multiple images in extensions")
    parser.add_option("--outfile",dest='outfile',
                      help="Destination of catalog output, default=stdout")
    parser.add_option("--table",dest='out_table',
                      help="Destination of catalog table output. Format infered from extension. default=None")
    parser.add_option("--rms",dest='rms',type='float',
                      help="Assume a single image noise of rms, default is to calculate a rms over regions of 20x20 beams")
    parser.add_option("--rmsin",dest='rmsinfile',
                      help="A .fits file that represents the image rms, usually created from --save_background.")
    parser.add_option('--bkgin',dest='bkginfile',
                      help="A .fits file that represents the background level, usually created from --save_background.")
    parser.add_option("--maxsummits",dest='max_summits',type='float',
                      help="If more than *maxsummits* summits are detected in an island, no fitting is done, only estimation. Default is None = always fit")
    parser.add_option("--csigma",dest='csigma',type='float',
                      help="The clipping value applied to the curvature map, when deciding which peaks/summits are significant. Default is None = calculate from image")
    parser.add_option('--seedclip',dest='innerclip',type='float',
                     help='The clipping value (in sigmas) for seeding islands. Default=5')
    parser.add_option('--floodclip',dest='outerclip',type='float',
                      help='The clipping value (in sigmas) for growing islands. Default=4')
    parser.add_option('--beam',dest='beam',type='float', nargs=3,
                      help='The beam parameters to be used is "--beam major minor pa" all in degrees. Default is to read from FITS header.')
    parser.add_option('--file_version',dest='file_version',action="store_true",
                      help='show the versions of each file')
    parser.add_option('--save_background', dest='save_background', action="store_true",
                      help='save the background/rms/curvature maps to aegean-background.fits, aegean-rms.fits, aegean-curvature.fits and exit')
    parser.add_option('--catalog',dest='catfile',
                      help='Catalog of locations at which fluxes will be measured. No source fitting is done. Many other options are ignored.')
    parser.add_option('--island',dest='doislandflux',action="store_true",
                      help='list the integrated island flux as well as the fitted component fluxes for each island')
    parser.set_defaults(debug=False,hdu_index=0,outfile=sys.stdout,out_table=None,rms=None,rmsinfile=None,bgkinfile=None,
                        max_summits=None,csigma=None,innerclip=5,outerclip=4,file_version=False,save_background=False,
                        catfile=None,beam=None,doislandflux=False)
    (options, args) = parser.parse_args()

    # configure logging
    logging_level = logging.DEBUG if options.debug else logging.INFO
    logging.basicConfig(level=logging_level, format="%(process)d:%(levelname)s %(message)s")
    logging.info("This is Aegean {0}".format(version))
    #debugging in multi core mode is very hard to understand
    if options.debug:
        logging.info("Setting cores=1 for debugging")
        options.cores=1
    if options.file_version:
        logging.info("Using aegean.py {0}".format(version))
        logging.info("Using fits_image.py {0}".format(FitsImage.version))
        sys.exit()
        
    if len(args)==0:
        parser.print_help()
        sys.exit()
    filename = args[0]
    if not os.path.exists(filename):
        logging.error( "{0} does not exist".format(filename))
        sys.exit()
    hdu_index = options.hdu_index
    if hdu_index > 0:
        logging.info( "Using hdu index {0}".format(hdu_index))
    
    #create a beam object from user input
    if options.beam is not None:
        beam=options.beam
        if len(beam)!=3:
            print "Beam requires 3 args. You supplied {0}".format(beam)
            sys.exit()
        options.beam=Beam(beam[0],beam[1],beam[2])
        
    # Generate and save the background FITS files and exit if requested
    if options.save_background:
        save_background_files(filename, hdu_index=hdu_index)
        sys.exit()

    if options.catfile and not os.path.exists(options.catfile):
        logging.error( "{0} does not exist".format(options.catfile))
        sys.exit()
        
    #Open the outfile
    if options.outfile is not sys.stdout:
        options.outfile=open(os.path.expanduser(options.outfile),'w')
     
    if options.bkginfile and not os.path.exists(options.bkginfile):
        logging.error("{0} not found".format(options.bkginfile))
        sys.exit()
    if options.rmsinfile and not os.path.exists(options.rmsinfile):
        logging.error("{0} not found".format(options.rmsinfile))
        sys.exit()
            
    if options.catfile:
        sources = measure_catalog_fluxes(filename, catfile=options.catfile, hdu_index=options.hdu_index,
                               outfile=options.outfile, bkgin=options.bkginfile,beam=options.beam)
    else:        
        sources = find_sources_in_image(filename, outfile=options.outfile, hdu_index=options.hdu_index,rms=options.rms,
                                    max_summits=options.max_summits,csigma=options.csigma,innerclip=options.innerclip,
                                    outerclip=options.outerclip, cores=options.cores, rmsin=options.rmsinfile, 
                                    bkgin=options.bkginfile,beam=options.beam, doislandflux=options.doislandflux)
    if options.out_table:
        save_catalog(options.out_table,sources)
    if len(sources) == 0:
        logging.info("No sources found in image")

