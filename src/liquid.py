""" @package forcebalance.liquid Matching of liquid bulk properties.  Under development.

author Lee-Ping Wang
@date 04/2012
"""

import abc
import os
import shutil
from forcebalance.finite_difference import *
from forcebalance.nifty import *
from forcebalance.nifty import _exec
from forcebalance.target import Target
import numpy as np
from forcebalance.molecule import Molecule
from re import match, sub
import subprocess
from subprocess import PIPE
try:
    from lxml import etree
except: pass
from pymbar import pymbar
import itertools
from collections import defaultdict, namedtuple, OrderedDict
from forcebalance.optimizer import Counter, GoodStep
import csv

from forcebalance.output import getLogger
logger = getLogger(__name__)

def weight_info(W, PT, N_k, verbose=True):
    C = []
    N = 0
    W += 1.0e-300
    I = np.exp(-1*np.sum((W*np.log(W))))
    for ns in N_k:
        C.append(sum(W[N:N+ns]))
        N += ns
    C = np.array(C)
    if verbose:
        logger.info("MBAR Results for Phase Point %s, Box, Contributions:\n" % str(PT))
        logger.info(str(C) + '\n')
        logger.info("InfoContent: % .2f snapshots (%.2f %%)\n" % (I, 100*I/len(W)))
    return C

# NPT_Trajectory = namedtuple('NPT_Trajectory', ['fnm', 'Rhos', 'pVs', 'Energies', 'Grads', 'mEnergies', 'mGrads', 'Rho_errs', 'Hvap_errs'])

class Liquid(Target):
    
    """ Subclass of Target for liquid property matching."""

    def __init__(self,options,tgt_opts,forcefield):
        # Initialize base class
        super(Liquid,self).__init__(options,tgt_opts,forcefield)
        # Fractional weight of the density
        self.set_option(tgt_opts,'w_rho',forceprint=True)
        # Fractional weight of the enthalpy of vaporization
        self.set_option(tgt_opts,'w_hvap',forceprint=True)
        # Fractional weight of the thermal expansion coefficient
        self.set_option(tgt_opts,'w_alpha',forceprint=True)
        # Fractional weight of the isothermal compressibility
        self.set_option(tgt_opts,'w_kappa',forceprint=True)
        # Fractional weight of the isobaric heat capacity
        self.set_option(tgt_opts,'w_cp',forceprint=True)
        # Fractional weight of the dielectric constant
        self.set_option(tgt_opts,'w_eps0',forceprint=True)
        # Optionally pause on the zeroth step
        self.set_option(tgt_opts,'manual')
        # Don't target the average enthalpy of vaporization and allow it to freely float (experimental)
        self.set_option(tgt_opts,'hvap_subaverage')
        # Number of time steps in the liquid "equilibration" run
        self.set_option(tgt_opts,'liquid_eq_steps',forceprint=True)
        # Number of time steps in the liquid "production" run
        self.set_option(tgt_opts,'liquid_md_steps',forceprint=True)
        # Number of time steps in the gas "equilibration" run
        self.set_option(tgt_opts,'gas_eq_steps',forceprint=True)
        # Number of time steps in the gas "production" run
        self.set_option(tgt_opts,'gas_md_steps',forceprint=True)
        # Time step length (in fs) for the liquid production run
        self.set_option(tgt_opts,'liquid_timestep',forceprint=True)
        # Time interval (in ps) for writing coordinates
        self.set_option(tgt_opts,'liquid_interval',forceprint=True)
        # Time step length (in fs) for the gas production run
        self.set_option(tgt_opts,'gas_timestep',forceprint=True)
        # Time interval (in ps) for writing coordinates
        self.set_option(tgt_opts,'gas_interval',forceprint=True)
        # Adjust simulation length in response to simulation uncertainty
        self.set_option(tgt_opts,'adapt_errors',forceprint=True)
        # Minimize the energy prior to running any dynamics
        self.set_option(tgt_opts,'minimize_energy',forceprint=True)
        # Isolated dipole (debye) for analytic self-polarization correction.
        self.set_option(tgt_opts,'self_pol_mu0',forceprint=True)
        # Molecular polarizability (ang**3) for analytic self-polarization correction.
        self.set_option(tgt_opts,'self_pol_alpha',forceprint=True)
        # Set up the simulation object for self-polarization correction.
        self.do_self_pol = (self.self_pol_mu0 > 0.0 and self.self_pol_alpha > 0.0)
        # Enable anisotropic periodic box
        self.set_option(tgt_opts,'anisotropic_box',forceprint=True)
        # Whether to save trajectories (0 = never, 1 = delete after good step, 2 = keep all)
        self.set_option(tgt_opts,'save_traj')

        #======================================#
        #     Variables which are set here     #
        #======================================#
        # This target can read data from disk.
        self.readable = True
        # Read in liquid starting coordinates.
        if not os.path.exists(os.path.join(self.root, self.tgtdir, self.liquid_coords)): 
            raise RuntimeError("%s doesn't exist; please provide liquid_coords option" % self.liquid_coords)
        self.liquid_mol = Molecule(os.path.join(self.root, self.tgtdir, self.liquid_coords))
        # Read in gas starting coordinates.
        if not os.path.exists(os.path.join(self.root, self.tgtdir, self.gas_coords)): 
            raise RuntimeError("%s doesn't exist; please provide gas_coords option" % self.gas_coords)
        self.gas_mol = Molecule(os.path.join(self.root, self.tgtdir, self.gas_coords))
        # List of trajectory files that may be deleted if self.save_traj == 1.
        self.last_traj = []
        # Extra files to be copied back at the end of a run.
        self.extra_output = []
        ## Read the reference data
        self.read_data()
        # Extra files to be linked into the temp-directory.
        self.nptfiles += [self.liquid_coords, self.gas_coords]
        # Scripts to be copied from the ForceBalance installation directory.
        self.scripts += ['npt.py']
        # Prepare the temporary directory.
        self.prepare_temp_directory()
        # Build keyword dictionary to pass to engine.
        if self.do_self_pol:
            self.gas_engine_args.update(self.OptionDict)
            self.gas_engine_args.update(options)
            del self.gas_engine_args['name']
            # Create engine object for gas molecule to do the polarization correction.
            self.gas_engine = self.engine_(target=self, mol=self.gas_mol, name="selfpol", **self.gas_engine_args)
        #======================================#
        #          UNDER DEVELOPMENT           #
        #======================================#
        # Put stuff here that I'm not sure about. :)
        np.set_printoptions(precision=4, linewidth=100)
        np.seterr(under='ignore')
        ## Saved trajectories for all iterations and all temperatures
        self.SavedTraj = defaultdict(dict)
        ## Evaluated energies for all trajectories (i.e. all iterations and all temperatures), using all mvals
        self.MBarEnergy = defaultdict(lambda:defaultdict(dict))
        ## Saved results for all iterations
        # self.SavedMVals = []
        self.AllResults = defaultdict(lambda:defaultdict(list))

    def prepare_temp_directory(self):
        """ Prepare the temporary directory by copying in important files. """
        abstempdir = os.path.join(self.root,self.tempdir)
        for f in self.nptfiles:
            LinkFile(os.path.join(self.root, self.tgtdir, f), os.path.join(abstempdir, f))
        for f in self.scripts:
            LinkFile(os.path.join(os.path.split(__file__)[0],"data",f),os.path.join(abstempdir,f))

    def read_data(self):
        # Read the 'data.csv' file. The file should contain guidelines.
        with open(os.path.join(self.tgtdir,'data.csv'),'rU') as f: R0 = list(csv.reader(f))
        # All comments are erased.
        R1 = [[sub('#.*$','',word) for word in line] for line in R0 if len(line[0]) > 0 and line[0][0] != "#"]
        # All empty lines are deleted and words are converted to lowercase.
        R = [[wrd.lower() for wrd in line] for line in R1 if any([len(wrd) for wrd in line]) > 0]
        global_opts = OrderedDict()
        found_headings = False
        known_vars = ['mbar','rho','hvap','alpha','kappa','cp','eps0','cvib_intra',
                      'cvib_inter','cni','devib_intra','devib_inter']
        self.RefData = OrderedDict()
        for line in R:
            if line[0] == "global":
                # Global options are mainly denominators for the different observables.
                if isfloat(line[2]):
                    global_opts[line[1]] = float(line[2])
                elif line[2].lower() == 'false':
                    global_opts[line[1]] = False
                elif line[2].lower() == 'true':
                    global_opts[line[1]] = True
            elif not found_headings:
                found_headings = True
                headings = line
                if len(set(headings)) != len(headings):
                    raise Exception('Column headings in data.csv must be unique')
                if 'p' not in headings:
                    raise Exception('There must be a pressure column heading labeled by "p" in data.csv')
                if 't' not in headings:
                    raise Exception('There must be a temperature column heading labeled by "t" in data.csv')
            elif found_headings:
                try:
                    # Temperatures are in kelvin.
                    t     = [float(val) for head, val in zip(headings,line) if head == 't'][0]
                    # For convenience, users may input the pressure in atmosphere or bar.
                    pval  = [float(val.split()[0]) for head, val in zip(headings,line) if head == 'p'][0]
                    punit = [val.split()[1] if len(val.split()) >= 1 else "atm" for head, val in zip(headings,line) if head == 'p'][0]
                    unrec = set([punit]).difference(['atm','bar']) 
                    if len(unrec) > 0:
                        raise Exception('The pressure unit %s is not recognized, please use bar or atm' % unrec[0])
                    # This line actually reads the reference data and inserts it into the RefData dictionary of dictionaries.
                    for head, val in zip(headings,line):
                        if head == 't' or head == 'p' : continue
                        if isfloat(val):
                            self.RefData.setdefault(head,OrderedDict([]))[(t,pval,punit)] = float(val)
                        elif val.lower() == 'true':
                            self.RefData.setdefault(head,OrderedDict([]))[(t,pval,punit)] = True
                        elif val.lower() == 'false':
                            self.RefData.setdefault(head,OrderedDict([]))[(t,pval,punit)] = False
                except:
                    logger.error(line + '\n')
                    raise Exception('Encountered an error reading this line!')
            else:
                logger.error(line + '\n')
                raise Exception('I did not recognize this line!')
        # Check the reference data table for validity.
        default_denoms = defaultdict(int)
        PhasePoints = None
        for head in self.RefData:
            if head not in known_vars+[i+"_wt" for i in known_vars]:
                # Only hard-coded properties may be recognized.
                raise Exception("The column heading %s is not recognized in data.csv" % head)
            if head in known_vars:
                if head+"_wt" not in self.RefData:
                    # If the phase-point weights are not specified in the reference data file, initialize them all to one.
                    self.RefData[head+"_wt"] = OrderedDict([(key, 1.0) for key in self.RefData[head]])
                wts = np.array(self.RefData[head+"_wt"].values())
                dat = np.array(self.RefData[head].values())
                avg = np.average(dat, weights=wts)
                if len(wts) > 1:
                    # If there is more than one data point, then the default denominator is the
                    # standard deviation of the experimental values.
                    default_denoms[head+"_denom"] = np.sqrt(np.dot(wts, (dat-avg)**2)/wts.sum())
                else:
                    # If there is only one data point, then the denominator is just the single
                    # data point itself.
                    default_denoms[head+"_denom"] = np.sqrt(np.abs(dat[0]))
            self.PhasePoints = self.RefData[head].keys()
            # This prints out all of the reference data.
            # printcool_dictionary(self.RefData[head],head)
        # Create labels for the directories.
        self.Labels = ["%.2fK-%.1f%s" % i for i in self.PhasePoints]
        self.req_pattern = self.Labels
        logger.debug("global_opts:\n%s\n" % str(global_opts))
        logger.debug("default_denoms:\n%s\n" % str(default_denoms))
        for opt in global_opts:
            if "_denom" in opt:
                # Record entries from the global_opts dictionary so they can be retrieved from other methods.
                self.set_option(global_opts,opt,default=default_denoms[opt])
            else:
                self.set_option(global_opts,opt)

    def npt_simulation(self, temperature, pressure, simnum):
        """ Submit a NPT simulation to the Work Queue. """
        wq = getWorkQueue()
        if not (os.path.exists('npt_result.p') or os.path.exists('npt_result.p.bz2')):
            link_dir_contents(os.path.join(self.root,self.rundir),os.getcwd())
            self.last_traj += [os.path.join(os.getcwd(), i) for i in self.extra_output]
            self.liquid_mol[simnum%len(self.liquid_mol)].write(self.liquid_coords, ftype='tinker' if self.engname == 'tinker' else None)
            cmdstr = '%s python npt.py %s %.3f %.3f' % (self.nptpfx, self.engname, temperature, pressure)
            if wq == None:
                logger.info("Running condensed phase simulation locally.\n")
                logger.info("You may tail -f %s/npt.out in another terminal window\n" % os.getcwd())
                _exec(cmdstr, copy_stderr=True, outfnm='npt.out')
            else:
                queue_up(wq, command = cmdstr+' &> npt.out',
                         input_files = self.nptfiles + self.scripts + ['forcebalance.p'],
                         output_files = ['npt_result.p.bz2', 'npt.out'] + self.extra_output, tgt=self)

    def polarization_correction(self,mvals):
        self.FF.make(mvals)
        ddict = self.gas_engine.multipole_moments(optimize=True)['dipole']
        d = np.array(ddict.values())
        if not in_fd():
            logger.info("The molecular dipole moment is % .3f debye\n" % np.linalg.norm(d))
        # Taken from the original OpenMM interface code, this is how we calculate the conversion factor.
        # dd2 = ((np.linalg.norm(d)-self.self_pol_mu0)*debye)**2
        # eps0 = 8.854187817620e-12 * coulomb**2 / newton / meter**2
        # epol = 0.5*dd2/(self.self_pol_alpha*angstrom**3*4*np.pi*eps0)/(kilojoule_per_mole/AVOGADRO_CONSTANT_NA)
        # In [2]: eps0 = 8.854187817620e-12 * coulomb**2 / newton / meter**2
        # In [7]: 1.0 * debye ** 2 / (1.0 * angstrom**3*4*np.pi*eps0) / (kilojoule_per_mole/AVOGADRO_CONSTANT_NA)
        # Out[7]: 60.240179789402056
        convert = 60.240179789402056
        dd2 = (np.linalg.norm(d)-self.self_pol_mu0)**2
        epol = 0.5*convert*dd2/self.self_pol_alpha
        return epol

    def indicate(self): 
        AGrad = hasattr(self, 'Gp')
        PrintDict = OrderedDict()
        def print_item(key, heading, physunit):
            if self.Xp[key] > 0:
                printcool_dictionary(self.Pp[key], title='%s %s%s\nTemperature  Pressure  Reference  Calculated +- Stdev     Delta    Weight    Term   ' % 
                                     (self.name, heading, " (%s) " % physunit if physunit else ""), bold=True, color=4, keywidth=15)
                bar = printcool("%s objective function: % .3f%s" % (heading, self.Xp[key], ", Derivative:" if AGrad else ""))
                if AGrad:
                    self.FF.print_map(vals=self.Gp[key])
                    logger.info(bar)
                PrintDict[heading] = "% 10.5f % 8.3f % 14.5e" % (self.Xp[key], self.Wp[key], self.Xp[key]*self.Wp[key])

        print_item("Rho", "Density", "kg m^-3")
        print_item("Hvap", "Enthalpy of Vaporization", "kJ mol^-1")
        print_item("Alpha", "Thermal Expansion Coefficient", "10^-4 K^-1")
        print_item("Kappa", "Isothermal Compressibility", "10^-6 bar^-1")
        print_item("Cp", "Isobaric Heat Capacity", "cal mol^-1 K^-1")
        print_item("Eps0", "Dielectric Constant", None)

        PrintDict['Total'] = "% 10s % 8s % 14.5e" % ("","",self.Objective)

        Title = "%s Condensed Phase Properties:\n %-20s %40s" % (self.name, "Property Name", "Residual x Weight = Contribution")
        printcool_dictionary(PrintDict,color=4,title=Title,keywidth=31)
        return

    def objective_term(self, points, expname, calc, err, grad, name="Quantity", SubAverage=False):
        if expname in self.RefData:
            exp = self.RefData[expname]
            Weights = self.RefData[expname+"_wt"]
            Denom = getattr(self,expname+"_denom")
        else:
            # If the reference data doesn't exist then return nothing.
            return 0.0, np.zeros(self.FF.np), np.zeros((self.FF.np,self.FF.np)), None
            
        Sum = sum(Weights.values())
        for i in Weights:
            Weights[i] /= Sum
        logger.info("Weights have been renormalized to " + str(sum(Weights.values())) + "\n")
        # Use least-squares or hyperbolic (experimental) objective.
        LeastSquares = True

        logger.info("Physical quantity %s uses denominator = % .4f\n" % (name, Denom))
        if not LeastSquares:
            # If using a hyperbolic functional form
            # we still want the contribution to the 
            # objective function to be the same when
            # Delta = Denom.
            Denom /= 3 ** 0.5
        
        Objective = 0.0
        Gradient = np.zeros(self.FF.np)
        Hessian = np.zeros((self.FF.np,self.FF.np))
        Objs = {}
        GradMap = []
        avgCalc = 0.0
        avgExp  = 0.0
        avgGrad = np.zeros(self.FF.np)
        for i, PT in enumerate(points):
            avgCalc += Weights[PT]*calc[PT]
            avgExp  += Weights[PT]*exp[PT]
            avgGrad += Weights[PT]*grad[PT]

        for i, PT in enumerate(points):
            if SubAverage:
                G = grad[PT]-avgGrad
                Delta = calc[PT] - exp[PT] - avgCalc + avgExp
            else:
                G = grad[PT]
                Delta = calc[PT] - exp[PT]
            if LeastSquares:
                # Least-squares objective function.
                ThisObj = Weights[PT] * Delta ** 2 / Denom**2
                Objs[PT] = ThisObj
                ThisGrad = 2.0 * Weights[PT] * Delta * G / Denom**2
                GradMap.append(G)
                Objective += ThisObj
                Gradient += ThisGrad
                # Gauss-Newton approximation to the Hessian.
                Hessian += 2.0 * Weights[PT] * (np.outer(G, G)) / Denom**2
            else:
                # L1-like objective function.
                D = Denom
                S = Delta**2 + D**2
                ThisObj  = Weights[PT] * (S**0.5-D) / Denom
                ThisGrad = Weights[PT] * (Delta/S**0.5) * G / Denom
                ThisHess = Weights[PT] * (1/S**0.5-Delta**2/S**1.5) * np.outer(G,G) / Denom
                Objs[PT] = ThisObj
                GradMap.append(G)
                Objective += ThisObj
                Gradient += ThisGrad
                Hessian += ThisHess
        GradMapPrint = [["#PhasePoint"] + self.FF.plist]
        for PT, g in zip(points,GradMap):
            GradMapPrint.append([' %8.2f %8.1f %3s' % PT] + ["% 9.3e" % i for i in g])
        o = wopen('gradient_%s.dat' % name)
        for line in GradMapPrint:
            print >> o, ' '.join(line)
        o.close()
            
        Delta = np.array([calc[PT] - exp[PT] for PT in points])
        delt = {PT : r for PT, r in zip(points,Delta)}
        print_out = OrderedDict([('    %8.2f %8.1f %3s' % PT,"%9.3f    %9.3f +- %-7.3f % 7.3f % 9.5f % 9.5f" % (exp[PT],calc[PT],err[PT],delt[PT],Weights[PT],Objs[PT])) for PT in calc])
        return Objective, Gradient, Hessian, print_out

    def submit_jobs(self, mvals, AGrad=True, AHess=True):
        # This routine is called by Objective.stage() will run before "get".
        # It submits the jobs to the Work Queue and the stage() function will wait for jobs to complete.
        #
        # First dump the force field to a pickle file
        printcool("Target: %s - launching MD simulations\nTime steps: %i (eq) + %i (md)" % (self.name, self.liquid_eq_steps, self.liquid_md_steps), color=0)
        with wopen('forcebalance.p') as f: lp_dump((self.FF,mvals,self.OptionDict,AGrad),f)

        # Give the user an opportunity to copy over data from a previous (perhaps failed) run.
        if Counter() == 0 and self.manual:
            warn_press_key("Now's our chance to fill the temp directory up with data!", timeout=7200)

        # If self.save_traj == 1, delete the trajectory files from a previous good optimization step.
        if Counter() > 0 and GoodStep() and self.save_traj < 2:
            for fn in self.last_traj:
                if os.path.exists(fn):
                    os.remove(fn)
        self.last_traj = []

        # Set up and run the NPT simulations.
        snum = 0
        for label, pt in zip(self.Labels, self.PhasePoints):
            T = pt[0]
            P = pt[1]
            Punit = pt[2]
            if Punit == 'bar':
                P *= 1.0 / 1.01325
            if not os.path.exists(label):
                os.makedirs(label)
                os.chdir(label)
                self.npt_simulation(T,P,snum)
                os.chdir('..')
                snum += 1

    def get(self, mvals, AGrad=True, AHess=True):
        
        """
        Fitting of liquid bulk properties.  This is the current major
        direction of development for ForceBalance.  Basically, fitting
        the QM energies / forces alone does not always give us the
        best simulation behavior.  In many cases it makes more sense
        to try and reproduce some experimentally known data as well.

        In order to reproduce experimentally known data, we need to
        run a simulation and compare the simulation result to
        experiment.  The main challenge here is that the simulations
        are computationally intensive (i.e. they require energy and
        force evaluations), and furthermore the results are noisy.  We
        need to run the simulations automatically and remotely
        (i.e. on clusters) and a good way to calculate the derivatives
        of the simulation results with respect to the parameter values.

        This function contains some experimentally known values of the
        density and enthalpy of vaporization (Hvap) of liquid water.
        It launches the density and Hvap calculations on the cluster,
        and gathers the results / derivatives.  The actual calculation
        of results / derivatives is done in a separate file.

        After the results come back, they are gathered together to form
        an objective function.

        @param[in] mvals Mathematical parameter values
        @param[in] AGrad Switch to turn on analytic gradient
        @param[in] AHess Switch to turn on analytic Hessian
        @return Answer Contribution to the objective function
        
        """

        mbar_verbose = True

        Answer = {}

        Results = {}
        Points = []  # These are the phase points for which data exists.
        BPoints = [] # These are the phase points for which we are doing MBAR for the condensed phase.
        mBPoints = [] # These are the phase points for which we are doing MBAR for the monomers.
        mPoints = [] # These are the phase points to use for enthalpy of vaporization; if we're scanning pressure then set hvap_wt for higher pressures to zero.
        tt = 0
        for label, PT in zip(self.Labels, self.PhasePoints):
            if os.path.exists('./%s/npt_result.p.bz2' % label):
                _exec('bunzip2 ./%s/npt_result.p.bz2' % label, print_command=False)
            elif os.path.exists('./%s/npt_result.p' % label): pass
            else:
                logger.warning('In %s :\n' % os.getcwd())
                logger.warning('The file ./%s/npt_result.p.bz2 does not exist so we cannot unzip it\n' % label)
            if os.path.exists('./%s/npt_result.p' % label):
                logger.info('Reading information from ./%s/npt_result.p\n' % label)
                Points.append(PT)
                Results[tt] = lp_load(open('./%s/npt_result.p' % label))
                if 'hvap' in self.RefData and PT[0] not in [i[0] for i in mPoints]:
                    mPoints.append(PT)
                if 'mbar' in self.RefData and PT in self.RefData['mbar'] and self.RefData['mbar'][PT]:
                    BPoints.append(PT)
                    if 'hvap' in self.RefData and PT[0] not in [i[0] for i in mBPoints]:
                        mBPoints.append(PT)
                tt += 1
            else:
                logger.warning('The file ./%s/npt_result.p does not exist so we cannot read it\n' % label)
                pass
                # for obs in self.RefData:
                #     del self.RefData[obs][PT]
        if len(Points) == 0:
            raise Exception('The liquid simulations have terminated with \x1b[1;91mno readable data\x1b[0m - this is a problem!')

        # Assign variable names to all the stuff in npt_result.p
        Rhos, Vols, Potentials, Energies, Dips, Grads, GDips, mPotentials, mEnergies, mGrads, \
            Rho_errs, Hvap_errs, Alpha_errs, Kappa_errs, Cp_errs, Eps0_errs, NMols = ([Results[t][i] for t in range(len(Points))] for i in range(17))
        # Determine the number of molecules
        if len(set(NMols)) != 1:
            logger.error(str(NMols))
            raise Exception('The above list should only contain one number - the number of molecules')
        else:
            NMol = list(set(NMols))[0]
    
        if not self.adapt_errors:
            self.AllResults = defaultdict(lambda:defaultdict(list))

        self.AllResults[tuple(mvals)]['E'].append(np.array(Energies))
        self.AllResults[tuple(mvals)]['V'].append(np.array(Vols))
        self.AllResults[tuple(mvals)]['R'].append(np.array(Rhos))
        self.AllResults[tuple(mvals)]['Dx'].append(np.array([d[:,0] for d in Dips]))
        self.AllResults[tuple(mvals)]['Dy'].append(np.array([d[:,1] for d in Dips]))
        self.AllResults[tuple(mvals)]['Dz'].append(np.array([d[:,2] for d in Dips]))
        self.AllResults[tuple(mvals)]['G'].append(np.array(Grads))
        self.AllResults[tuple(mvals)]['GDx'].append(np.array([gd[0] for gd in GDips]))
        self.AllResults[tuple(mvals)]['GDy'].append(np.array([gd[1] for gd in GDips]))
        self.AllResults[tuple(mvals)]['GDz'].append(np.array([gd[2] for gd in GDips]))
        self.AllResults[tuple(mvals)]['L'].append(len(Energies[0]))
        self.AllResults[tuple(mvals)]['Steps'].append(self.liquid_md_steps)

        if len(mPoints) > 0:
            self.AllResults[tuple(mvals)]['mE'].append(np.array([i for pt, i in zip(Points,mEnergies) if pt in mPoints]))
            self.AllResults[tuple(mvals)]['mG'].append(np.array([i for pt, i in zip(Points,mGrads) if pt in mPoints]))

        # Number of data sets belonging to this value of the parameters.
        Nrpt = len(self.AllResults[tuple(mvals)]['R'])
        sumsteps = sum(self.AllResults[tuple(mvals)]['Steps'])
        if self.liquid_md_steps != sumsteps:
            printcool("This objective function evaluation combines %i datasets\n" \
                          "Increasing simulation length: %i -> %i steps" % \
                          (Nrpt, self.liquid_md_steps, sumsteps), color=6)
            if self.liquid_md_steps * 2 != sumsteps:
                raise RuntimeError("Spoo!")
            self.liquid_eq_steps *= 2
            self.liquid_md_steps *= 2
            self.gas_eq_steps *= 2
            self.gas_md_steps *= 2


        # Concatenate along the data-set axis (more than 1 element  if we've returned to these parameters.)
        E, V, R, Dx, Dy, Dz = \
            (np.hstack(tuple(self.AllResults[tuple(mvals)][i])) for i in \
                 ['E', 'V', 'R', 'Dx', 'Dy', 'Dz'])
        # for i in self.AllResults[tuple(mvals)]['G']:
        #     print i[0, 0, :]
        G, GDx, GDy, GDz = \
            (np.hstack((np.concatenate(tuple(self.AllResults[tuple(mvals)][i]), axis=2))) for i in ['G', 'GDx', 'GDy', 'GDz'])

        # All temperatures, first parameter
        # print G[0, :]
        print E.shape
        print G.shape
        
        if len(mPoints) > 0:
            mE = np.hstack(tuple(self.AllResults[tuple(mvals)]['mE']))
            mG = np.hstack((np.concatenate(tuple(self.AllResults[tuple(mvals)]['mG']), axis=2)))
        Rho_calc = OrderedDict([])
        Rho_grad = OrderedDict([])
        Rho_std  = OrderedDict([])
        Hvap_calc = OrderedDict([])
        Hvap_grad = OrderedDict([])
        Hvap_std  = OrderedDict([])
        Alpha_calc = OrderedDict([])
        Alpha_grad = OrderedDict([])
        Alpha_std  = OrderedDict([])
        Kappa_calc = OrderedDict([])
        Kappa_grad = OrderedDict([])
        Kappa_std  = OrderedDict([])
        Cp_calc = OrderedDict([])
        Cp_grad = OrderedDict([])
        Cp_std  = OrderedDict([])
        Eps0_calc = OrderedDict([])
        Eps0_grad = OrderedDict([])
        Eps0_std  = OrderedDict([])

        # The unit that converts atmospheres * nm**3 into kj/mol :)
        pvkj=0.061019351687175

        # Run MBAR using the total energies. Required for estimates that use the kinetic energy.
        BSims = len(BPoints)
        Shots = len(E[0])
        N_k = np.ones(BSims)*Shots
        # Use the value of the energy for snapshot t from simulation k at potential m
        U_kln = np.zeros([BSims,BSims,Shots])
        for m, PT in enumerate(BPoints):
            T = PT[0]
            P = PT[1] / 1.01325 if PT[2] == 'bar' else PT[1]
            beta = 1. / (kb * T)
            for k in range(BSims):
                # The correct Boltzmann factors include PV.
                # Note that because the Boltzmann factors are computed from the conditions at simulation "m",
                # the pV terms must be rescaled to the pressure at simulation "m".
                kk = Points.index(BPoints[k])
                U_kln[k, m, :]   = E[kk] + P*V[kk]*pvkj
                U_kln[k, m, :]  *= beta
        W1 = None
        if len(BPoints) > 1:
            logger.info("Running MBAR analysis on %i states...\n" % len(BPoints))
            mbar = pymbar.MBAR(U_kln, N_k, verbose=mbar_verbose, relative_tolerance=5.0e-8)
            W1 = mbar.getWeights()
            logger.info("Done\n")
        elif len(BPoints) == 1:
            W1 = np.ones((BPoints*Shots,BPoints))
            W1 /= BPoints*Shots
        
        def fill_weights(weights, phase_points, mbar_points, snapshots):
            """ Fill in the weight matrix with MBAR weights where MBAR was run, 
            and equal weights otherwise. """
            new_weights = np.zeros([len(phase_points)*snapshots,len(phase_points)])
            for m, PT in enumerate(phase_points):
                if PT in mbar_points:
                    mm = mbar_points.index(PT)
                    for kk, PT1 in enumerate(mbar_points):
                        k = phase_points.index(PT1)
                        logger.debug("Will fill W2[%i:%i,%i] with W1[%i:%i,%i]\n" % (k*snapshots,k*snapshots+snapshots,m,kk*snapshots,kk*snapshots+snapshots,mm))
                        new_weights[k*snapshots:(k+1)*snapshots,m] = weights[kk*snapshots:(kk+1)*snapshots,mm]
                else:
                    logger.debug("Will fill W2[%i:%i,%i] with equal weights\n" % (m*snapshots,(m+1)*snapshots,m))
                    new_weights[m*snapshots:(m+1)*snapshots,m] = 1.0/snapshots
            return new_weights
        
        W2 = fill_weights(W1, Points, BPoints, Shots)
        print W2.shape

        if len(mPoints) > 0:
            # Run MBAR on the monomers.  This is barely necessary.
            mW1 = None
            mShots = len(mE[0])
            if len(mBPoints) > 0:
                mBSims = len(mBPoints)
                mN_k = np.ones(mBSims)*mShots
                mU_kln = np.zeros([mBSims,mBSims,mShots])
                for m, PT in enumerate(mBPoints):
                    T = PT[0]
                    beta = 1. / (kb * T)
                    for k in range(mBSims):
                        kk = Points.index(mBPoints[k])
                        mU_kln[k, m, :]  = mE[kk]
                        mU_kln[k, m, :] *= beta
                if np.abs(np.std(mE)) > 1e-6 and mBSims > 1:
                    mmbar = pymbar.MBAR(mU_kln, mN_k, verbose=False, relative_tolerance=5.0e-8, method='self-consistent-iteration')
                    mW1 = mmbar.getWeights()
            elif len(mBPoints) == 1:
                mW1 = np.ones((mBSims*mShots,mSims))
                mW1 /= mBSims*mShots
            mW2 = fill_weights(mW1, mPoints, mBPoints, mShots)
         
        if self.do_self_pol:
            EPol = self.polarization_correction(mvals)
            GEPol = np.array([f12d3p(fdwrap(self.polarization_correction, mvals, p), h = self.h, f0 = EPol)[0] for p in range(self.FF.np)])
            bar = printcool("Self-polarization correction to \nenthalpy of vaporization is % .3f kJ/mol%s" % (EPol, ", Derivative:" if AGrad else ""))
            if AGrad:
                self.FF.print_map(vals=GEPol)
                logger.info(bar)

        # Arrays must be flattened now for calculation of properties.
        E = E.flatten()
        V = V.flatten()
        R = R.flatten()
        Dx = Dx.flatten()
        Dy = Dy.flatten()
        Dz = Dz.flatten()
        if len(mPoints) > 0: mE = mE.flatten()
            
        for i, PT in enumerate(Points):
            T = PT[0]
            P = PT[1] / 1.01325 if PT[2] == 'bar' else PT[1]
            PV = P*V*pvkj
            H = E + PV
            # The weights that we want are the last ones.
            W = flat(W2[:,i])
            C = weight_info(W, PT, np.ones(len(Points))*Shots, verbose=mbar_verbose)
            Gbar = flat(np.matrix(G)*col(W))
            mBeta = -1/kb/T
            Beta  = 1/kb/T
            kT    = kb*T
            # Define some things to make the analytic derivatives easier.
            def avg(vec):
                return np.dot(W,vec)
            def covde(vec):
                return flat(np.matrix(G)*col(W*vec)) - avg(vec)*Gbar
            def deprod(vec):
                return flat(np.matrix(G)*col(W*vec))
            ## Density.
            Rho_calc[PT]   = np.dot(W,R)
            Rho_grad[PT]   = mBeta*(flat(np.matrix(G)*col(W*R)) - np.dot(W,R)*Gbar)
            ## Enthalpy of vaporization.
            if PT in mPoints:
                ii = mPoints.index(PT)
                mW = flat(mW2[:,ii])
                mGbar = flat(np.matrix(mG)*col(mW))
                Hvap_calc[PT]  = np.dot(mW,mE) - np.dot(W,E)/NMol + kb*T - np.dot(W, PV)/NMol
                Hvap_grad[PT]  = mGbar + mBeta*(flat(np.matrix(mG)*col(mW*mE)) - np.dot(mW,mE)*mGbar)
                Hvap_grad[PT] -= (Gbar + mBeta*(flat(np.matrix(G)*col(W*E)) - np.dot(W,E)*Gbar)) / NMol
                Hvap_grad[PT] -= (mBeta*(flat(np.matrix(G)*col(W*PV)) - np.dot(W,PV)*Gbar)) / NMol
                if self.do_self_pol:
                    Hvap_calc[PT] -= EPol
                    Hvap_grad[PT] -= GEPol
                if hasattr(self,'use_cni') and self.use_cni:
                    if not ('cni' in self.RefData and self.RefData['cni'][PT]):
                        raise RuntimeError('Asked for a nonideality correction but not provided in reference data (data.csv).  Either disable the option in data.csv or add data.')
                    logger.debug("Adding % .3f to enthalpy of vaporization at " % self.RefData['cni'][PT] + str(PT) + '\n')
                    Hvap_calc[PT] += self.RefData['cni'][PT]
                if hasattr(self,'use_cvib_intra') and self.use_cvib_intra:
                    if not ('cvib_intra' in self.RefData and self.RefData['cvib_intra'][PT]):
                        raise RuntimeError('Asked for a quantum intramolecular vibrational correction but not provided in reference data (data.csv).  Either disable the option in data.csv or add data.')
                    logger.debug("Adding % .3f to enthalpy of vaporization at " % self.RefData['cvib_intra'][PT] + str(PT) + '\n')
                    Hvap_calc[PT] += self.RefData['cvib_intra'][PT]
                if hasattr(self,'use_cvib_inter') and self.use_cvib_inter:
                    if not ('cvib_inter' in self.RefData and self.RefData['cvib_inter'][PT]):
                        raise RuntimeError('Asked for a quantum intermolecular vibrational correction but not provided in reference data (data.csv).  Either disable the option in data.csv or add data.')
                    logger.debug("Adding % .3f to enthalpy of vaporization at " % self.RefData['cvib_inter'][PT] + str(PT) + '\n')
                    Hvap_calc[PT] += self.RefData['cvib_inter'][PT]
            else:
                Hvap_calc[PT]  = 0.0
                Hvap_grad[PT]  = np.zeros(self.FF.np)
            ## Thermal expansion coefficient.
            Alpha_calc[PT] = 1e4 * (avg(H*V)-avg(H)*avg(V))/avg(V)/(kT*T)
            GAlpha1 = -1 * Beta * deprod(H*V) * avg(V) / avg(V)**2
            GAlpha2 = +1 * Beta * avg(H*V) * deprod(V) / avg(V)**2
            GAlpha3 = deprod(V)/avg(V) - Gbar
            GAlpha4 = Beta * covde(H)
            Alpha_grad[PT] = 1e4 * (GAlpha1 + GAlpha2 + GAlpha3 + GAlpha4)/(kT*T)
            ## Isothermal compressibility.
            bar_unit = 0.06022141793 * 1e6
            Kappa_calc[PT] = bar_unit / kT * (avg(V**2)-avg(V)**2)/avg(V)
            GKappa1 = +1 * Beta**2 * avg(V**2) * deprod(V) / avg(V)**2
            GKappa2 = -1 * Beta**2 * avg(V) * deprod(V**2) / avg(V)**2
            GKappa3 = +1 * Beta**2 * covde(V)
            Kappa_grad[PT] = bar_unit*(GKappa1 + GKappa2 + GKappa3)
            ## Isobaric heat capacity.
            Cp_calc[PT] = 1000/(4.184*NMol*kT*T) * (avg(H**2) - avg(H)**2)
            if hasattr(self,'use_cvib_intra') and self.use_cvib_intra:
                logger.debug("Adding " + str(self.RefData['devib_intra'][PT]) + " to the heat capacity\n")
                Cp_calc[PT] += self.RefData['devib_intra'][PT]
            if hasattr(self,'use_cvib_inter') and self.use_cvib_inter:
                logger.debug("Adding " + str(self.RefData['devib_inter'][PT]) + " to the heat capacity\n")
                Cp_calc[PT] += self.RefData['devib_inter'][PT]
            GCp1 = 2*covde(H) * 1000 / 4.184 / (NMol*kT*T)
            GCp2 = mBeta*covde(H**2) * 1000 / 4.184 / (NMol*kT*T)
            GCp3 = 2*Beta*avg(H)*covde(H) * 1000 / 4.184 / (NMol*kT*T)
            Cp_grad[PT] = GCp1 + GCp2 + GCp3
            ## Static dielectric constant.
            prefactor = 30.348705333964077
            D2 = avg(Dx**2)+avg(Dy**2)+avg(Dz**2)-avg(Dx)**2-avg(Dy)**2-avg(Dz)**2
            Eps0_calc[PT] = 1.0 + prefactor*(D2/avg(V))/T
            GD2  = 2*(flat(np.matrix(GDx)*col(W*Dx)) - avg(Dx)*flat(np.matrix(GDx)*col(W))) - Beta*(covde(Dx**2) - 2*avg(Dx)*covde(Dx))
            GD2 += 2*(flat(np.matrix(GDy)*col(W*Dy)) - avg(Dy)*flat(np.matrix(GDy)*col(W))) - Beta*(covde(Dy**2) - 2*avg(Dy)*covde(Dy))
            GD2 += 2*(flat(np.matrix(GDz)*col(W*Dz)) - avg(Dz)*flat(np.matrix(GDz)*col(W))) - Beta*(covde(Dz**2) - 2*avg(Dz)*covde(Dz))
            Eps0_grad[PT] = prefactor*(GD2/avg(V) - mBeta*covde(V)*D2/avg(V)**2)/T
            ## Estimation of errors.
            Rho_std[PT]    = np.sqrt(sum(C**2 * np.array(Rho_errs)**2))
            if PT in mPoints:
                Hvap_std[PT]   = np.sqrt(sum(C**2 * np.array(Hvap_errs)**2))
            else:
                Hvap_std[PT]   = 0.0
            Alpha_std[PT]   = np.sqrt(sum(C**2 * np.array(Alpha_errs)**2)) * 1e4
            Kappa_std[PT]   = np.sqrt(sum(C**2 * np.array(Kappa_errs)**2)) * 1e6
            Cp_std[PT]   = np.sqrt(sum(C**2 * np.array(Cp_errs)**2))
            Eps0_std[PT]   = np.sqrt(sum(C**2 * np.array(Eps0_errs)**2))

        # Get contributions to the objective function
        X_Rho, G_Rho, H_Rho, RhoPrint = self.objective_term(Points, 'rho', Rho_calc, Rho_std, Rho_grad, name="Density")
        X_Hvap, G_Hvap, H_Hvap, HvapPrint = self.objective_term(Points, 'hvap', Hvap_calc, Hvap_std, Hvap_grad, name="H_vap", SubAverage=self.hvap_subaverage)
        X_Alpha, G_Alpha, H_Alpha, AlphaPrint = self.objective_term(Points, 'alpha', Alpha_calc, Alpha_std, Alpha_grad, name="Thermal Expansion")
        X_Kappa, G_Kappa, H_Kappa, KappaPrint = self.objective_term(Points, 'kappa', Kappa_calc, Kappa_std, Kappa_grad, name="Compressibility")
        X_Cp, G_Cp, H_Cp, CpPrint = self.objective_term(Points, 'cp', Cp_calc, Cp_std, Cp_grad, name="Heat Capacity")
        X_Eps0, G_Eps0, H_Eps0, Eps0Print = self.objective_term(Points, 'eps0', Eps0_calc, Eps0_std, Eps0_grad, name="Dielectric Constant")

        Gradient = np.zeros(self.FF.np)
        Hessian = np.zeros((self.FF.np,self.FF.np))

        if X_Rho == 0: self.w_rho = 0.0
        if X_Hvap == 0: self.w_hvap = 0.0
        if X_Alpha == 0: self.w_alpha = 0.0
        if X_Kappa == 0: self.w_kappa = 0.0
        if X_Cp == 0: self.w_cp = 0.0
        if X_Eps0 == 0: self.w_eps0 = 0.0

        w_tot = self.w_rho + self.w_hvap + self.w_alpha + self.w_kappa + self.w_cp + self.w_eps0
        w_1 = self.w_rho / w_tot
        w_2 = self.w_hvap / w_tot
        w_3 = self.w_alpha / w_tot
        w_4 = self.w_kappa / w_tot
        w_5 = self.w_cp / w_tot
        w_6 = self.w_eps0 / w_tot

        Objective    = w_1 * X_Rho + w_2 * X_Hvap + w_3 * X_Alpha + w_4 * X_Kappa + w_5 * X_Cp + w_6 * X_Eps0
        if AGrad:
            Gradient = w_1 * G_Rho + w_2 * G_Hvap + w_3 * G_Alpha + w_4 * G_Kappa + w_5 * G_Cp + w_6 * G_Eps0
        if AHess:
            Hessian  = w_1 * H_Rho + w_2 * H_Hvap + w_3 * H_Alpha + w_4 * H_Kappa + w_5 * H_Cp + w_6 * H_Eps0

        if not in_fd():
            self.Xp = {"Rho" : X_Rho, "Hvap" : X_Hvap, "Alpha" : X_Alpha, 
                           "Kappa" : X_Kappa, "Cp" : X_Cp, "Eps0" : X_Eps0}
            self.Wp = {"Rho" : w_1, "Hvap" : w_2, "Alpha" : w_3, 
                           "Kappa" : w_4, "Cp" : w_5, "Eps0" : w_6}
            self.Pp = {"Rho" : RhoPrint, "Hvap" : HvapPrint, "Alpha" : AlphaPrint, 
                           "Kappa" : KappaPrint, "Cp" : CpPrint, "Eps0" : Eps0Print}
            if AGrad:
                self.Gp = {"Rho" : G_Rho, "Hvap" : G_Hvap, "Alpha" : G_Alpha, 
                               "Kappa" : G_Kappa, "Cp" : G_Cp, "Eps0" : G_Eps0}
            self.Objective = Objective

        Answer = {'X':Objective, 'G':Gradient, 'H':Hessian}
        return Answer
