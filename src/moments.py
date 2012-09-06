""" @package moments Multipole moment fitting module

@author Lee-Ping Wang
@date 09/2012
"""

import os
import shutil
from nifty import col, eqcgmx, flat, floatornan, fqcgmx, invert_svd, kb, printcool, bohrang, warn_press_key
from numpy import append, array, diag, dot, exp, log, mat, mean, ones, outer, sqrt, where, zeros, linalg, savetxt, hstack
from fitsim import FittingSimulation
from molecule import Molecule, format_xyz_coord
from re import match, sub
import subprocess
import itertools
from subprocess import PIPE
from finite_difference import fdwrap, f1d2p, f12d3p, in_fd
from collections import OrderedDict

class Moments(FittingSimulation):

    """ Subclass of FittingSimulation for fitting force fields to multipole moments (from experiment or theory).

    Currently Tinker is supported.

    """
    
    def __init__(self,options,sim_opts,forcefield):
        """Initialization."""
        
        # Initialize the SuperClass!
        super(Moments,self).__init__(options,sim_opts,forcefield)
        
        #======================================#
        # Options that are given by the parser #
        #======================================#
	self.denoms = {}
        self.denoms['dipole'] = sim_opts['dipole_denom']
        self.denoms['quadrupole'] = sim_opts['quadrupole_denom']
        
        #======================================#
        #     Variables which are set here     #
        #======================================#
        ## The mdata.txt file that contains the moments.
        self.mfnm = os.path.join(self.simdir,"mdata.txt")
        ##
        self.ref_moments = OrderedDict()
        ## Read in the reference data
        self.read_reference_data()
        ## Prepare the temporary directory
        self.prepare_temp_directory(options,sim_opts)

    def read_reference_data(self):
        """ Read the reference data from a file. """
        ## Number of atoms
        self.na = -1
        self.ref_eigvals = []
        self.ref_eigvecs = []
        an = 0
        ln = 0
        cn = -1
        dn = -1
        qn = -1
        for line in open(self.mfnm):
            line = line.split('#')[0] # Strip off comments
            s = line.split()
            if len(s) == 0:
                pass
            elif len(s) == 1 and self.na == -1:
                self.na = int(s[0])
                xyz = zeros((self.na, 3), dtype=float)
                cn = ln + 1
            elif ln == cn:
                pass
            elif an < self.na and len(s) == 4:
                xyz[an, :] = array([float(i) for i in s[1:]])
                an += 1
            elif an == self.na and s[0].lower() == 'dipole':
                dn = ln + 1
            elif ln == dn:
                self.ref_moments['dipole'] = OrderedDict(zip(['x','y','z'],[float(i) for i in s]))
            elif an == self.na and s[0].lower() in ['quadrupole', 'quadrapole']:
                qn = ln + 1
            elif ln == qn:
                self.ref_moments['quadrupole'] = OrderedDict([('xx',float(s[0]))])
            elif qn > 0 and ln == qn + 1:
                self.ref_moments['quadrupole']['xy'] = float(s[0])
                self.ref_moments['quadrupole']['yy'] = float(s[1])
            elif qn > 0 and ln == qn + 2:
                self.ref_moments['quadrupole']['xz'] = float(s[0])
                self.ref_moments['quadrupole']['yz'] = float(s[1])
                self.ref_moments['quadrupole']['zz'] = float(s[2])
            else:
                print line
                raise Exception("This line doesn't comply with our multipole file format!")
            ln += 1

        return

    def prepare_temp_directory(self, options, sim_opts):
        """ Prepare the temporary directory, by default does nothing (gmxx2 needs it) """
        return
        
    def indicate(self):
        """ Print qualitative indicator. """
        print "\rSim: %-15s" % self.name, 
        print "Multipole Moments"
        print "Reference :", self.ref_moments
        print "Calculated:", self.calc_moments
        print "Objective = %.5e" % self.objective
        return

    def unpack_moments(self, moment_dict):
        answer = array(list(itertools.chain(*[[dct[i]/self.denoms[ord] for i in dct] for ord,dct in moment_dict.items()])))
        return answer

    def get(self, mvals, AGrad=False, AHess=False):
        """ Evaluate objective function. """
        Answer = {'X':0.0, 'G':zeros(self.FF.np, dtype=float), 'H':zeros((self.FF.np, self.FF.np), dtype=float)}
        def get_momvals(mvals_):
            self.FF.make(mvals_)
            moments = self.moments_driver()
            # Unpack from dictionary.
            return self.unpack_moments(moments)

        self.FF.make(mvals)
        ref_momvals = self.unpack_moments(self.ref_moments)
        calc_moments = self.moments_driver()
        calc_momvals = self.unpack_moments(calc_moments)

        D = calc_momvals - ref_momvals
        dV = zeros((self.FF.np,len(calc_momvals)),dtype=float)

        if AGrad or AHess:
            for p in range(self.FF.np):
                dV[p,:], _ = f12d3p(fdwrap(get_momvals, mvals, p), h = self.h, f0 = calc_momvals)
                
        Answer['X'] = dot(D,D)
        for p in range(self.FF.np):
            Answer['G'][p] = 2*dot(D, dV[p,:])
            for q in range(self.FF.np):
                Answer['H'][p,q] = 2*dot(dV[p,:], dV[q,:])

        if not in_fd():
            self.FF.make(mvals)
            self.calc_moments = calc_moments
            self.objective = Answer['X']

        return Answer