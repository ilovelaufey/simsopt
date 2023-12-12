#!/usr/bin/env python

"""
Example script for the force metric in a stage-two coil optimization
"""
import time
import os
from pathlib import Path
from scipy.optimize import minimize
import numpy as np
from simsopt.geo import curves_to_vtk, create_equally_spaced_curves
from simsopt.geo import SurfaceRZFourier
from simsopt.field import Current, coils_via_symmetries
from simsopt.objectives import SquaredFlux
from simsopt.geo import CurveLength, CurveCurveDistance, CurveSurfaceDistance
from simsopt.field import BiotSavart
from simsopt.field.force import MeanSquaredForceOpt
from simsopt.field.selffield import regularization_circ


# File for the desired boundary magnetic surface:
TEST_DIR = (Path(__file__).parent / ".." / ".." / "tests" / "test_files").resolve()
filename = TEST_DIR / 'input.LandremanPaul2021_QA'

# Directory for output
OUT_DIR = "./output/"
# OUT_DIR = "/Users/sienahurwitz/Documents/Academic/UMD/Plasma Physics Research/force research/Code/python simsopt stuff/output"
os.makedirs(OUT_DIR, exist_ok=True)

ncoils = 4
R0 = 1
R1 = 0.8
order = 3

LENGTH_WEIGHT = 1e-5

CC_THRESHOLD = 0.1
CC_WEIGHT = 1e-1

CS_THRESHOLD = 0.4
CS_WEIGHT = 10

FORCE_WEIGHT = 0  # 1e-13

config_str = f"{ncoils}_coils_force_weight_{FORCE_WEIGHT}"
#######################################################
# End of input parameters.
#######################################################


# Initialize the boundary magnetic surface:
nphi = 32
ntheta = 32
s = SurfaceRZFourier.from_vmec_input(
    filename, range="half period", nphi=nphi, ntheta=ntheta)

# Create the initial coils:
base_curves = create_equally_spaced_curves(
    ncoils, s.nfp, stellsym=True, R0=R0, R1=R1, order=order, numquadpoints=15)
base_currents = [Current(2.5e5) for i in range(ncoils)]
# Since the target field is zero, one possible solution is just to set all
# currents to 0. To avoid the minimizer finding that solution, we fix one
# of the currents:
base_currents[0].fix_all()
coils = coils_via_symmetries(base_curves, base_currents, s.nfp, True)
base_coils = coils[:ncoils]

bs = BiotSavart(coils)
bs.set_points(s.gamma().reshape((-1, 3)))

curves = [c.curve for c in coils]
curves_to_vtk(curves, OUT_DIR + "/curves_init")
pointData = {"B_N": np.sum(bs.B().reshape(
    (nphi, ntheta, 3)) * s.unitnormal(), axis=2)[:, :, None]}
s.to_vtk(OUT_DIR + "surf_init", extra_data=pointData)

# Define the individual terms objective function:
Jf = SquaredFlux(s, bs)
Jls = [CurveLength(c) for c in base_curves]
Jccdist = CurveCurveDistance(curves, CC_THRESHOLD, num_basecurves=ncoils)
Jcsdist = CurveSurfaceDistance(curves, s, CS_THRESHOLD)
# Jforce = MaxForceOpt(base_coils, coils, regularization_circ(0.05))
Jforce = MeanSquaredForceOpt(base_coils, coils, regularization_circ(0.05))
# Jforce = LpForceOpt(base_coils, coils, regularization_circ(0.05), 2)


# base_currents[0].unfix_all()
JF = Jf + Jforce * FORCE_WEIGHT

MAXITER = 10
dofs = JF.x

def fun(dofs):
    JF.x = dofs
    J = JF.J()
    grad = JF.dJ()
    print(f"J={J:.3e}, ||∇J||={np.linalg.norm(grad):.3e}, J_force={Jforce.J():.3e}, Jflux={Jf.J():.3e}", flush=True)
    return J, grad

print("Beginning optimization without force objective")
res = minimize(fun, dofs, jac=True, method='L-BFGS-B',
               options={'maxiter': MAXITER, 'maxcor': 300}, tol=1e-15)
curves_to_vtk(base_curves, OUT_DIR + f"curves_opt_{config_str}")

dofs = res.x
print("Beginning optimization with force objective")
# FORCE_WEIGHT += 5*1e-9
FORCE_WEIGHT += 5*1e9
JF = Jf + Jforce * FORCE_WEIGHT
print(FORCE_WEIGHT)

res = minimize(fun, dofs, jac=True, method='L-BFGS-B',
               options={'maxiter': MAXITER, 'maxcor': 300}, tol=1e-15)

curves_to_vtk(base_curves, OUT_DIR + f"curves_opt_force_{config_str}")

pointData = {"B_N": np.sum(bs.B().reshape(
    (nphi, ntheta, 3)) * s.unitnormal(), axis=2)[:, :, None]/np.linalg.norm(bs.B().reshape(
        (nphi, ntheta, 3)), axis=2)}
s.to_vtk(OUT_DIR + f"surf_opt_{config_str}", extra_data=pointData)