#imports
import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm
import os
try:
    import cupy as cp
    use_gpu = True
except:
    use_gpu = False
import pickle
import time

from stableemrifisher.fisher import StableEMRIFisher
from stableemrifisher.utils import inner_product, generate_PSD, padding

from few.utils.utility import get_p_at_t
from few.trajectory.inspiral import EMRIInspiral
from few.waveform import GenerateEMRIWaveform
from few.summation.aakwave import AAKSummation
from few.utils.constants import YRSID_SI 
from few.utils.constants import SPEED_OF_LIGHT as C_SI

from fastlisaresponse import ResponseWrapper  # Response function 
from lisatools.detector import ESAOrbits #ESAOrbits correspond to esa-trailing-orbits.h5

from scipy.integrate import quad, nquad
from scipy.interpolate import RegularGridInterpolator, CubicSpline
from scipy.stats import uniform
from scipy.special import factorial
from scipy.optimize import brentq, root

from scipy.stats import multivariate_normal
import warnings

if not use_gpu:
    cfg_set = few.get_config_setter(reset=True)
    cfg_set.enable_backends("cpu")
    cfg_set.set_log_level("info")
else:
    pass #let the backend decide for itself.

def H(z,H0,Omega_m0,Omega_Lambda0):
    """
    calculate the Hubble parameter at redshift z given H0 (in the same units as H0)
    """
    return H0 * np.sqrt(Omega_m0*(1+z)**3 + Omega_Lambda0)

def integrand_dc(z,H0,Omega_m0,Omega_Lambda0):
    return C_SI/H(z,H0,Omega_m0,Omega_Lambda0)

def dc(z,H0,Omega_m0,Omega_Lambda0):
    """
    returns the comoving distance in Gpc for a given redshift z
    """
    return quad(integrand_dc,0,z,args=(H0,Omega_m0,Omega_Lambda0),epsabs=1e-1,epsrel=1e-1)[0]

def getdist(z,H0,Omega_m0,Omega_Lambda0):
    """
    returns the luminosity distance for a given redshift z
    """
    return (1+z)*dc(z,H0,Omega_m0,Omega_Lambda0)

def dlminusdistz(z, dl, H0,Omega_m0,Omega_Lambda0):
    return dl - getdist(z,H0,Omega_m0,Omega_Lambda0)

def getz(dl,H0,Omega_m0,Omega_Lambda0):
    """
    returns the redshift for a given luminosity distance
    """    
    return (root(dlminusdistz,x0=0.1,args=(dl,H0,Omega_m0,Omega_Lambda0)).x)[0]
    
def Jacobian(M,dist,H0,Omega_m0,Omega_Lambda0):
    """ 
    Jacobian for Fisher parameter transformation from [M,dist,Al,nl,Ag] to [M,z,Al,nl,Ag]
    Returns a 5x5 diagonal np.ndarray.
    """
    
    #Jacobian = partial old/partial new
    
    delta = dist*1e-5
    del_z_del_dist = ((getz(dist+delta,H0,Omega_m0,Omega_Lambda0)-getz(dist-delta,H0,Omega_m0,Omega_Lambda0))/(2*delta))
    diag = np.diag((1.0,(del_z_del_dist)**-1,1.0,1.0,1.0))
    
    return diag

def fishinv(M, Fisher, index_of_M = 0):
    """ 
    Calculate the Fisher inverse by transforming the index of M to lnM to improve conditionality of the matrix first. 
    ONLY WORKS WITH INPUTS THAT HAVE PARAM M AT INDEX "index_of_M"!
    Helps with stability of Fisher inversion.
    """
    
    #Jacobian for Fisher = partial old/partial new, going from M -> lnM
    
    J = np.eye(len(Fisher))
    J[index_of_M,index_of_M] = M

    Fisher_lnM = J.T @ Fisher @ J

    Fisher_lnM_inv = np.linalg.inv(Fisher_lnM)

    #Jacobian for Covariance = partial new/partial old, going from lnM -> M

    Fisher_inv = J.T @ Fisher_lnM_inv @ J
    
    return Fisher_inv
    
#supporting function
def check_prior(param,bound):
    """ return True if param within bound (including edges), False otherwise """

    if (param >= bound[0]) & (param <= bound[1]):
        return 0 #within bounds
    elif (param < bound[0]):
        return -1 #lower bound hit
    else:
        return 1 #upper bound hit
    
def out_of_bounds_allparams(params, bounds):
    """ return False if all params within bounds, False otherwise
        params = list or nd.array of all parameters
        bounds = dict with names of params as keys and their bounds [lower, upper] as the values"""

    out_of_bounds = False
    
    for param,j in zip(bounds.keys(),range(len(bounds.keys()))):
        if np.abs(check_prior(params[j],bounds[param])) == 1: #if the source parameters hits the upper limit
            out_of_bounds = True
            
    return out_of_bounds
