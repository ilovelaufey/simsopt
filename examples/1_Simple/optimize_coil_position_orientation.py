#!/usr/bin/env python
r"""In this example we solve a stage II optimiyation problem 
where the main toroidal field is generated by circular toroidal
coils, and shaping is provided by windowpane coils.

The objective is given by

    J = (1/2) \int |B dot n|^2 ds

The target equilibrium is the QA configuration of arXiv:2108.03711.
The 

Better coils can be obtained by freeing some of the coils geometry
dofs, including additional penalty functions, like 
"""

import os
from pathlib import Path
import numpy as np
from scipy.optimize import minimize

from simsopt.geo import SurfaceRZFourier, create_equally_spaced_windowpane_curves, \
    CurveLength, curves_to_vtk, create_equally_spaced_curves
from simsopt.field import Current, coils_via_symmetries, BiotSavart
from simsopt.objectives import SquaredFlux
from simsopt.util import in_github_actions
from simsopt.field.coil import ScaledCurrent

# Number of unique coil shapes, i.e. the number of coils per half field period:
# (Since the configuration has nfp = 2, multiply by 4 to get the total number of coils.)
n_wp_coils = 2
n_tf_coils = 4

# Major radius for the initial circular coils:
R0 = 1.0

# Minor radius for the initial circular coils:
R1 = 0.5

# Number of iterations to perform:
MAXITER = 50 if in_github_actions else 300

# File for the desired boundary magnetic surface:
TEST_DIR = (Path(__file__).parent / ".." / ".." / "tests" / "test_files").resolve()
filename = TEST_DIR / 'input.LandremanPaul2021_QA'

# Directory for output
OUT_DIR = "./output/"
os.makedirs(OUT_DIR, exist_ok=True)

#######################################################
# End of input parameters.
#######################################################
# Initialize the boundary magnetic surface:
nphi = 32
ntheta = 32
s = SurfaceRZFourier.from_vmec_input(filename, range="half period", nphi=nphi, ntheta=ntheta)

# Create the initial coils:
base_tf_curves = create_equally_spaced_curves(n_tf_coils, s.nfp, stellsym=True, R0=R0, R1=R1, order=2)
base_wp_curves = create_equally_spaced_windowpane_curves(n_wp_coils, s.nfp, True, R0=(R0+R1)*1.01, R1=R1/10, Z0=0, order=2)

# We scale the currents so that dofs have all the same order of magnitude
base_tf_currents = [ScaledCurrent(Current(1.0), 1e5) for i in range(n_tf_coils)]
base_wp_currents = [ScaledCurrent(Current(1.0), 1e3) for i in range(n_wp_coils)] # we expect a smaller current in the wp coils

# We fix the tf coils geometry
for c in base_tf_curves:
    c.fix_all()

# We also fix the wp coils geometry, but keep their position and orientation unfixed
for c in base_wp_curves:
    c.fix_all()
    for xyz in ['x','y','z']:
        c.unfix(f'{xyz}0')
    for ypr in ['yaw', 'pitch', 'roll']:
        c.unfix(f'{ypr}')

# We unfix all currents
for c in base_tf_currents:
    c.unfix_all()
for c in base_wp_currents:
    c.unfix_all()

# Since the target field is zero, one possible solution is just to set all
# currents to 0. To avoid the minimizer finding that solution, we fix one
# of the currents:
base_tf_currents[0].fix_all()

tf_coils = coils_via_symmetries(base_tf_curves, base_tf_currents, s.nfp, True)
wp_coils = coils_via_symmetries(base_wp_curves, base_wp_currents, s.nfp, True)
coils = tf_coils + wp_coils
bs = BiotSavart(coils)
bs.set_points(s.gamma().reshape((-1, 3)))

curves = [c.curve for c in coils]
curves_to_vtk(curves, OUT_DIR + "curves_init", close=True)
pointData = {"B_N": np.sum(bs.B().reshape((nphi, ntheta, 3)) * s.unitnormal(), axis=2)[:, :, None]}
s.to_vtk(OUT_DIR + "surf_init", extra_data=pointData)

# Define the individual terms objective function:
Jf = SquaredFlux(s, bs)

# Form the total objective function. To do this, we can exploit the
# fact that Optimizable objects with J() and dJ() functions can be
# multiplied by scalars and added:
JF = Jf

B_dot_n = np.sum(bs.B().reshape((nphi, ntheta, 3)) * s.unitnormal(), axis=2)
print('Initial max|B dot n|:', np.max(np.abs(B_dot_n)))
print('Names of the dofs:', JF.dof_names)

# We don't have a general interface in SIMSOPT for optimisation problems that
# are not in least-squares form, so we write a little wrapper function that we
# pass directly to scipy.optimize.minimize


def fun(dofs):
    JF.x = dofs
    return JF.J(), JF.dJ()


print("""
################################################################################
### Perform a Taylor test ######################################################
################################################################################
""")
f = fun
dofs = JF.x
np.random.seed(1)
h = np.random.uniform(size=dofs.shape)
J0, dJ0 = f(dofs)
dJh = sum(dJ0 * h)
for eps in [1e-3, 1e-4, 1e-5, 1e-6, 1e-7]:
    J1, _ = f(dofs + eps*h)
    J2, _ = f(dofs - eps*h)
    print("err", (J1-J2)/(2*eps) - dJh)

print("""
################################################################################
### Run the optimisation #######################################################
################################################################################
""")
res = minimize(fun, dofs, jac=True, method='L-BFGS-B',
               options={'maxiter': MAXITER, 'maxcor': 300, 'iprint': 5}, tol=1e-15)
curves_to_vtk(curves, OUT_DIR + f"curves_opt", close=True)
pointData = {"B_N": np.sum(bs.B().reshape((nphi, ntheta, 3)) * s.unitnormal(), axis=2)[:, :, None]}
s.to_vtk(OUT_DIR + "surf_opt", extra_data=pointData)

B_dot_n = np.sum(bs.B().reshape((nphi, ntheta, 3)) * s.unitnormal(), axis=2)
print('Final max|B dot n|:', np.max(np.abs(B_dot_n)))

# Save the optimized coil shapes and currents so they can be loaded into other scripts for analysis:
bs.save(OUT_DIR + "biot_savart_opt.json")