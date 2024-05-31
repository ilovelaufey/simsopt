from pathlib import Path
import warnings

import numpy as np
from pyevtk.hl import pointsToVTK

from . import Surface
import simsoptpp as sopp
from simsopt.field import BiotSavart
import time
from scipy.linalg import inv, pinv, pinvh

__all__ = ['PSCgrid']
contig = np.ascontiguousarray

class PSCgrid:
    r"""
    ``PSCgrid`` is a class for setting up the grid, normal vectors,
    plasma surface, and other objects needed to perform PSC
    optimization for stellarators. The class
    takes as input either (1) two toroidal surfaces specified as 
    SurfaceRZFourier objects, and initializes a set of points 
    (in Cartesian coordinates) between these surfaces or (2) a manual
    list of xyz locations for the coils, for which the user needs to make
    sure that coils are non-intersecting even when they are arbitrarily
    rotated around. 
    """

    def __init__(self):
        self.mu0 = 4 * np.pi * 1e-7
        self.fac = 1e-7
        self.BdotN2_list = []
        # Define a set of quadrature points and weights for the N point
        # Gaussian quadrature rule
        num_quad = 20  # 20 required for unit tests to pass
        (quad_points_phi, 
         quad_weights) = np.polynomial.legendre.leggauss(num_quad)
        self.quad_points_phi = contig(quad_points_phi * np.pi + np.pi)
        self.quad_weights = contig(quad_weights)
        self.quad_points_rho = contig(quad_points_phi * 0.5 + 0.5)
        # rho = quad_points_phi * 0.5 + 0.5
        # self.Rho, self.Phi = np.meshgrid(rho, self.quad_points_phi, indexing='ij')
        # self.quad_points_phi = np.linspace(0, 2 * np.pi, num_quad)
        # self.quad_weights = np.ones(num_quad)
        # self.quad_dphi = self.quad_points_phi[1] - self.quad_points_phi[0]

    def _setup_uniform_grid(self):
        """
        Initializes a uniform grid in cartesian coordinates and sets
        some important grid variables for later.
        """
        # Get (X, Y, Z) coordinates of the two boundaries
        self.xyz_inner = self.inner_toroidal_surface.gamma().reshape(-1, 3)
        self.xyz_outer = self.outer_toroidal_surface.gamma().reshape(-1, 3)
        x_outer = self.xyz_outer[:, 0]
        y_outer = self.xyz_outer[:, 1]
        z_outer = self.xyz_outer[:, 2]

        x_max = np.max(x_outer)
        x_min = np.min(x_outer)
        y_max = np.max(y_outer)
        y_min = np.min(y_outer)
        z_max = np.max(z_outer)
        z_min = np.min(z_outer)
        z_max = max(z_max, abs(z_min))

        # Initialize uniform grid
        Nx = self.Nx
        Ny = self.Ny
        Nz = self.Nz
        self.dx = (x_max - x_min) / (Nx - 1)
        self.dy = (y_max - y_min) / (Ny - 1)
        self.dz = 2 * z_max / (Nz - 1)
        Nmin = min(self.dx, min(self.dy, self.dz))
        
        # This is not a guarantee that coils will not touch but inductance
        # matrix blows up if they do so it is easy to tell when they do
        self.R = Nmin / 4.0  #, self.poff / 2.5)  # self.dx / 2.0 #
        self.a = self.R / 100.0  # Hard-coded aspect ratio of 100 right now
        print('Major radius of the coils is R = ', self.R)
        print('Coils are spaced so that every coil of radius R '
              ' is at least 2R away from the next coil'
        )

        if self.plasma_boundary.nfp > 1:
            # Throw away any points not in the section phi = [0, pi / n_p] and
            # make sure all centers points are at least a distance R from the
            # sector so that all the coil points are reflected correctly. 
            X = np.linspace(
                self.dx / 2.0 + x_min, x_max - self.dx / 2.0, 
                Nx, endpoint=True
            )
            Y = np.linspace(
                self.dy / 2.0 + y_min, y_max - self.dy / 2.0, 
                Ny, endpoint=True
            )
        else:
            X = np.linspace(x_min, x_max, Nx, endpoint=True)
            Y = np.linspace(y_min, y_max, Ny, endpoint=True)
        Z = np.linspace(-z_max, z_max, Nz, endpoint=True)

        # Make 3D mesh
        X, Y, Z = np.meshgrid(X, Y, Z, indexing='ij')
        self.xyz_uniform = np.transpose(np.array([X, Y, Z]), [1, 2, 3, 0]).reshape(Nx * Ny * Nz, 3)
        
        # Extra work for nfp > 1 to chop off points outside sector
        # This is probably not robust for every stellarator but seems to work
        # reasonably well for the Landreman/Paul QA/QH in the code. 
        if self.nfp > 1:
            inds = []
            for i in range(Nx):
                for j in range(Ny):
                    for k in range(Nz):
                        phi = np.arctan2(Y[i, j, k], X[i, j, k])
                        # if self.nfp == 4:
                            # phi2 = np.arctan2(self.R / 2.0, X[i, j, k])
                        # elif self.nfp == 2:
                        phi2 = np.arctan2(self.R / 2.0, self.plasma_boundary.get_rc(0, 0))
                        # Add a little factor to avoid phi = pi / n_p degrees 
                        # exactly, which can intersect with a symmetrized
                        # coil if not careful 
                        # print(phi2)
                        # exit()
                        if phi >= (np.pi / self.nfp - phi2) or phi < 0.0:
                            inds.append(int(i * Ny * Nz + j * Nz + k))
            good_inds = np.setdiff1d(np.arange(Nx * Ny * Nz), inds)
            self.xyz_uniform = self.xyz_uniform[good_inds, :]

        # Save uniform grid before we start chopping off parts.
        pointsToVTK('uniform_grid', contig(self.xyz_uniform[:, 0]),
                    contig(self.xyz_uniform[:, 1]), contig(self.xyz_uniform[:, 2]))

    @classmethod
    def geo_setup_between_toroidal_surfaces(
        cls, 
        plasma_boundary : Surface,
        coils_TF,
        inner_toroidal_surface: Surface, 
        outer_toroidal_surface: Surface,
        **kwargs,
    ):
        """
        Function to initialize a SIMSOPT PSCgrid from a 
        volume defined by two toroidal surfaces. These must be specified
        directly. Often a good choice is made by extending the plasma 
        boundary by its normal vectors.

        Args
        ----------
        plasma_boundary: Surface class object 
            Representing the plasma boundary surface. Gets converted
            into SurfaceRZFourier object for ease of use.
        coils_TF: List of SIMSOPT Coil class objects
            A list of Coil class objects representing all of the toroidal field
            coils (including those obtained by applying the discrete 
            symmetries) needed to generate the induced currents in a PSC 
            array. 
        inner_toroidal_surface: Surface class object 
            Representing the inner toroidal surface of the volume.
            Gets converted into SurfaceRZFourier object for 
            ease of use.
        outer_toroidal_surface: Surface object representing
            the outer toroidal surface of the volume. Typically 
            want this to have same quadrature points as the inner
            surface for a functional grid setup. 
            Gets converted into SurfaceRZFourier object for 
            ease of use.
        kwargs: The following are valid keyword arguments.
            out_dir: string
                File name to specify an output directory for the plots and
                optimization results generated using this class object. 
            Nx: int
                Number of points in x to use in a cartesian grid, taken between the 
                inner and outer toroidal surfaces. 
            Ny: int
                Number of points in y to use in a cartesian grid, taken between the 
                inner and outer toroidal surfaces. 
            Nz: int
                Number of points in z to use in a cartesian grid, taken between the 
                inner and outer toroidal surfaces. 
            Bn_plasma: 2D numpy array, shape (ntheta_quadpoints, nphi_quadpoints)
                Magnetic field (only plasma) at the plasma
                boundary. Typically this will be the optimized plasma
                magnetic field from finite bootstrap current. 
                Set to zero if not specified by the user.
            ppp: int
                Order of the CurvePlanarFourier object used to represent and
                plot a PSC coil in real space.
            interpolated_field: bool
                Flag to indicate if the class should initialize an 
                InterpolatedField object from the TF coils, rather than a 
                true BiotSavart object. Speeds up lots of the calculations
                but comes with some accuracy reduction.
            random_initialization: bool
                Flag to indicate if the angles of each PSC should be 
                randomly initialized or not. If not, they are initialized so
                that their normal vectors align with the local B field 
                generated by the toroidal fields coils, generating essentially
                a maximum induced current in each PSC.
        Returns
        -------
        psc_grid: An initialized PSCgrid class object.

        """
        from simsopt.field import InterpolatedField, BiotSavart
        from simsopt.util import calculate_on_axis_B
        from . import curves_to_vtk
                
        psc_grid = cls() 
        psc_grid.out_dir = kwargs.pop("out_dir", '')
        
        # Get all the TF coils data
        psc_grid.I_TF = np.array([coil.current.get_value() for coil in coils_TF])
        psc_grid.dl_TF = np.array([coil.curve.gammadash() for coil in coils_TF])
        psc_grid.gamma_TF = np.array([coil.curve.gamma() for coil in coils_TF])
        TF_curves = np.array([coil.curve for coil in coils_TF])
        curves_to_vtk(TF_curves, psc_grid.out_dir + "coils_TF", close=True, scalar_data=psc_grid.I_TF)

        # Get all the geometric data from the plasma boundary
        psc_grid.plasma_boundary = plasma_boundary.to_RZFourier()
        psc_grid.nfp = plasma_boundary.nfp
        psc_grid.stellsym = plasma_boundary.stellsym
        psc_grid.symmetry = plasma_boundary.nfp * (plasma_boundary.stellsym + 1)
        if psc_grid.stellsym:
            psc_grid.stell_list = [1, -1]
        else:
            psc_grid.stell_list = [1]
        psc_grid.nphi = len(psc_grid.plasma_boundary.quadpoints_phi)
        psc_grid.ntheta = len(psc_grid.plasma_boundary.quadpoints_theta)
        psc_grid.plasma_unitnormals = contig(psc_grid.plasma_boundary.unitnormal().reshape(-1, 3))
        psc_grid.plasma_points = contig(psc_grid.plasma_boundary.gamma().reshape(-1, 3))
        Ngrid = psc_grid.nphi * psc_grid.ntheta
        Nnorms = np.ravel(np.sqrt(np.sum(psc_grid.plasma_boundary.normal() ** 2, axis=-1)))
        psc_grid.grid_normalization = np.sqrt(Nnorms / Ngrid)
        Bn_plasma = kwargs.pop("Bn_plasma", 
                               np.zeros((psc_grid.nphi, psc_grid.ntheta))
        )
        if not np.allclose(Bn_plasma.shape, (psc_grid.nphi, psc_grid.ntheta)): 
            raise ValueError('Plasma magnetic field surface data is incorrect shape.')
        psc_grid.Bn_plasma = Bn_plasma
        psc_grid.plasma_boundary_full = kwargs.pop("plasma_boundary_full", psc_grid.plasma_boundary)
        
        # Get geometric data for initializing the PSCs
        N = 20  # Number of integration points for integrals over PSC coils
        psc_grid.phi = np.linspace(0, 2 * np.pi, N, endpoint=False)
        psc_grid.dphi = psc_grid.phi[1] - psc_grid.phi[0]
        Nx = kwargs.pop("Nx", 10)
        Ny = kwargs.pop("Ny", Nx)
        Nz = kwargs.pop("Nz", Nx)
        psc_grid.poff = kwargs.pop("poff", 100)
        if Nx <= 0 or Ny <= 0 or Nz <= 0:
            raise ValueError('Nx, Ny, and Nz should be positive integers')
        psc_grid.Nx = Nx
        psc_grid.Ny = Ny
        psc_grid.Nz = Nz
        psc_grid.inner_toroidal_surface = inner_toroidal_surface.to_RZFourier()
        psc_grid.outer_toroidal_surface = outer_toroidal_surface.to_RZFourier()    
        warnings.warn(
            'Plasma boundary and inner and outer toroidal surfaces should '
            'all have the same "range" parameter in order for a PSC'
            ' array to be correctly initialized.'
        )        
        
        t1 = time.time()
        # Use the geometric info to initialize a grid of PSCs
        normal_inner = inner_toroidal_surface.unitnormal().reshape(-1, 3)   
        normal_outer = outer_toroidal_surface.unitnormal().reshape(-1, 3)   
        psc_grid._setup_uniform_grid()
        # Have the uniform grid, now need to loop through and eliminate cells.
        psc_grid.grid_xyz = sopp.define_a_uniform_cartesian_grid_between_two_toroidal_surfaces(
            contig(normal_inner), 
            contig(normal_outer), 
            contig(psc_grid.xyz_uniform), 
            contig(psc_grid.xyz_inner), 
            contig(psc_grid.xyz_outer)
        )
        inds = np.ravel(np.logical_not(np.all(psc_grid.grid_xyz == 0.0, axis=-1)))
        psc_grid.grid_xyz = np.array(psc_grid.grid_xyz[inds, :], dtype=float)
        psc_grid.num_psc = psc_grid.grid_xyz.shape[0]
        # psc_grid.rho = np.linspace(0, psc_grid.R, N, endpoint=False)
        psc_grid.quad_points_rho = psc_grid.quad_points_rho * psc_grid.R
        # psc_grid.drho = psc_grid.rho[1] - psc_grid.rho[0]
        # Initialize 2D (rho, phi) mesh for integrals over the PSC "face".
        # Rho, Phi = np.meshgrid(psc_grid.rho, psc_grid.phi, indexing='ij')
        # psc_grid.Rho = np.ravel(Rho)
        # psc_grid.Phi = np.ravel(Phi)
        
        # Order of each PSC coil when they are initialized as 
        # CurvePlanarFourier objects. For unit tests, needs > 400 for 
        # convergence.
        psc_grid.ppp = kwargs.pop("ppp", 100)
        
        # Setup the B field from the TF coils
        # Many big calls to B_TF.B() so can make an interpolated object
        # if accuracy is not paramount
        interpolated_field = kwargs.pop("interpolated_field", False)
        B_TF = BiotSavart(coils_TF)
        if interpolated_field:
            n = 40  # tried 20 here and then there are small errors in f_B 
            degree = 3
            R = psc_grid.R
            gamma_outer = outer_toroidal_surface.gamma().reshape(-1, 3)
            rs = np.linalg.norm(gamma_outer[:, :2], axis=-1)
            zs = gamma_outer[:, 2]
            rrange = (0, np.max(rs) + R, n)  # need to also cover the plasma
            if psc_grid.nfp > 1:
                phirange = (0, 2*np.pi/psc_grid.nfp, n*2)
            else:
                phirange = (0, 2 * np.pi, n * 2)
            if psc_grid.stellsym:
                zrange = (0, np.max(zs) + R, n // 2)
            else:
                zrange = (np.min(zs) - R, np.max(zs) + R, n // 2)
            B_TF = InterpolatedField(
                B_TF, degree, rrange, phirange, zrange, 
                True, nfp=psc_grid.nfp, stellsym=psc_grid.stellsym
            )
        B_TF.set_points(psc_grid.grid_xyz)
        B_axis = calculate_on_axis_B(B_TF, psc_grid.plasma_boundary, print_out=False)
        # Normalization of the ||A*Linv*psi - b||^2 objective 
        # representing Bnormal errors on the plasma surface
        psc_grid.normalization = B_axis ** 2 * psc_grid.plasma_boundary.area()
        psc_grid.fac2_norm = psc_grid.fac ** 2 / psc_grid.normalization
        psc_grid.B_TF = B_TF

        # Random or B_TF aligned initialization of the coil orientations
        initialization = kwargs.pop("initialization", "TF")
        if initialization == "random":
            # Randomly initialize the coil orientations
            psc_grid.alphas = (np.random.rand(psc_grid.num_psc) - 0.5) * 2 * np.pi
            psc_grid.deltas = (np.random.rand(psc_grid.num_psc) - 0.5) * 2 * np.pi
            psc_grid.coil_normals = np.array(
                [np.cos(psc_grid.alphas) * np.sin(psc_grid.deltas),
                  -np.sin(psc_grid.alphas),
                  np.cos(psc_grid.alphas) * np.cos(psc_grid.deltas)]
            ).T
            # deal with -0 terms in the normals, which screw up the arctan2 calculations
            psc_grid.coil_normals[
                np.logical_and(np.isclose(psc_grid.coil_normals, 0.0), 
                               np.copysign(1.0, psc_grid.coil_normals) < 0)
                ] *= -1.0
        elif initialization == "plasma":
            # determine the alphas and deltas from the plasma normal vectors
            psc_grid.coil_normals = np.zeros(psc_grid.grid_xyz.shape)
            for i in range(psc_grid.num_psc):
                point = psc_grid.grid_xyz[i, :]
                dists = np.sum((point - psc_grid.plasma_points) ** 2, axis=-1)
                # print(dists)
                # min_dist = 1000.0
                # for j in range(psc_grid.plasma_points.shape[0]):
                #     dist = np.sum((point - psc_grid.plasma_points[j, :]) ** 2)
                #     if dist < min_dist:
                #         min_dist = dist
                min_ind = np.argmin(dists)
                # print(dists[min_ind])
                psc_grid.coil_normals[i, :] = psc_grid.plasma_unitnormals[min_ind, :]
                
            psc_grid.deltas = np.arctan2(psc_grid.coil_normals[:, 0], 
                                         psc_grid.coil_normals[:, 2])
            psc_grid.deltas[abs(psc_grid.deltas) == np.pi] = 0.0
            psc_grid.alphas = -np.arctan2(
                psc_grid.coil_normals[:, 1] * np.cos(psc_grid.deltas), 
                psc_grid.coil_normals[:, 2]
            )
            psc_grid.alphas[abs(psc_grid.alphas) == np.pi] = 0.0
        elif initialization == "TF":
            # determine the alphas and deltas from these normal vectors
            B = B_TF.B()
            psc_grid.coil_normals = (B.T / np.sqrt(np.sum(B ** 2, axis=-1))).T
            psc_grid.deltas = np.arctan2(psc_grid.coil_normals[:, 0], 
                                         psc_grid.coil_normals[:, 2])
            psc_grid.deltas[abs(psc_grid.deltas) == np.pi] = 0.0
            psc_grid.alphas = -np.arctan2(
                psc_grid.coil_normals[:, 1] * np.cos(psc_grid.deltas), 
                psc_grid.coil_normals[:, 2]
            )
            psc_grid.alphas[abs(psc_grid.alphas) == np.pi] = 0.0
            
        # deal with -0 terms in the normals, which screw up the arctan2 calculations
        psc_grid.coil_normals[
            np.logical_and(np.isclose(psc_grid.coil_normals, 0.0), 
                           np.copysign(1.0, psc_grid.coil_normals) < 0)
            ] *= -1.0
            
        # Generate all the locations of the PSC coils obtained by applying
        # discrete symmetries (stellarator and field-period symmetries)
        psc_grid.setup_full_grid()
        t2 = time.time()
        # print('Initialize grid time = ', t2 - t1)
        
        # Initialize curve objects corresponding to each PSC coil for 
        # plotting in 3D
        # t1 = time.time()
        # Initialize all of the fields, currents, inductances, for all the PSCs
        psc_grid.setup_orientations(psc_grid.alphas, psc_grid.deltas)
        psc_grid.update_psi()
        # psc_grid.L_inv = inv(psc_grid.L, check_finite=False)
        # psc_grid.I = -psc_grid.L_inv[:psc_grid.num_psc, :psc_grid.num_psc] @ psc_grid.psi / psc_grid.fac
        psc_grid.setup_currents_and_fields()
        # t2 = time.time()
        # print('Geo setup time = ', t2 - t1)
        
        # Initialize CurvePlanarFourier objects for the PSCs, mostly for
        # plotting purposes
        psc_grid.setup_curves()
        psc_grid.plot_curves()
        
        # Set up vector b appearing in objective ||A*Linv*psi - b||^2
        # since it only needs to be initialized once and then stays fixed.
        psc_grid.b_vector()
        
        # Initialize kappas = [alphas, deltas] which is the array of
        # optimization variables used in this work. 
        kappas = np.ravel(np.array([psc_grid.alphas, psc_grid.deltas]))
        psc_grid.kappas = kappas
        return psc_grid
    
    @classmethod
    def geo_setup_manual(
        cls, 
        points,
        R,
        **kwargs,
    ):
        """
        Function to manually initialize a SIMSOPT PSCgrid by explicitly 
        passing: a list of points representing the center of each PSC coil, 
        the major and minor radius of each coil (assumed all the same), 
        and optionally the initial orientations of all the coils. This 
        initialization generates a generic TF field if coils_TF is not passed.

        Args
        ----------
        points: 2D numpy array, shape (num_psc, 3)
            A set of points specifying the center of every PSC coil in the 
            array.
        R: double
            Major radius of every PSC coil.
        kwargs: The following are valid keyword arguments.
            out_dir: string
                File name to specify an output directory for the plots and
                optimization results generated using this class object. 
            Nx: int
                Number of points in x to use in a cartesian grid, taken between the 
                inner and outer toroidal surfaces. 
            Ny: int
                Number of points in y to use in a cartesian grid, taken between the 
                inner and outer toroidal surfaces. 
            Nz: int
                Number of points in z to use in a cartesian grid, taken between the 
                inner and outer toroidal surfaces. 
            Bn_plasma: 2D numpy array, shape (ntheta_quadpoints, nphi_quadpoints)
                Magnetic field (only plasma) at the plasma
                boundary. Typically this will be the optimized plasma
                magnetic field from finite bootstrap current. 
                Set to zero if not specified by the user.
            ppp: int
                Order of the CurvePlanarFourier object used to represent and
                plot a PSC coil in real space.
            interpolated_field: bool
                Flag to indicate if the class should initialize an 
                InterpolatedField object from the TF coils, rather than a 
                true BiotSavart object. Speeds up lots of the calculations
                but comes with some accuracy reduction.
            coils_TF: List of SIMSOPT Coil class objects
                A list of Coil class objects representing all of the toroidal field
                coils (including those obtained by applying the discrete 
                symmetries) needed to generate the induced currents in a PSC 
                array. 
            a: double
                Minor radius of every PSC coil. Defaults to R / 100. 
            plasma_boundary: Surface class object 
                Representing the plasma boundary surface. Gets converted
                into SurfaceRZFourier object for ease of use. Defaults
                to using a low resolution QA stellarator. 
        Returns
        -------
        psc_grid: An initialized PSCgrid class object.

        """
        from simsopt.util.permanent_magnet_helper_functions import initialize_coils, calculate_on_axis_B
        from simsopt.geo import SurfaceRZFourier, create_equally_spaced_curves
        from simsopt.field import Current, coils_via_symmetries
        from simsopt.field import InterpolatedField
        from . import curves_to_vtk
        
        psc_grid = cls()
        # Initialize geometric information of the PSC array
        psc_grid.grid_xyz = np.array(points, dtype=float)
        psc_grid.R = R
        psc_grid.a = kwargs.pop("a", R / 100.0)
        psc_grid.out_dir = kwargs.pop("out_dir", '')
        psc_grid.num_psc = psc_grid.grid_xyz.shape[0]
        psc_grid.alphas = kwargs.pop("alphas", 
                                     (np.random.rand(
                                         psc_grid.num_psc) - 0.5) * 2 * np.pi
        )
        psc_grid.deltas = kwargs.pop("deltas", 
                                     (np.random.rand(
                                         psc_grid.num_psc) - 0.5) * 2 * np.pi
        )
        # N = 500  # Must be larger for some unit tests to converge
        # psc_grid.phi = np.linspace(0, 2 * np.pi, N, endpoint=False)
        # psc_grid.dphi = psc_grid.phi[1] - psc_grid.phi[0]
        # psc_grid.rho = np.linspace(0, R, N, endpoint=False)
        # psc_grid.drho = psc_grid.rho[1] - psc_grid.rho[0]
        # psc_grid.rho = np.linspace(0, psc_grid.R, N, endpoint=False)
        psc_grid.quad_points_rho = psc_grid.quad_points_rho * psc_grid.R
        # print(psc_grid.quad_points_rho)
        # Initialize 2D (rho, phi) mesh for integrals over the PSC "face".
        # Rho, Phi = np.meshgrid(psc_grid.rho, psc_grid.phi, indexing='ij')
        # psc_grid.Rho = np.ravel(Rho)
        # psc_grid.Phi = np.ravel(Phi)
    
        # initialize a default plasma boundary
        input_name = 'input.LandremanPaul2021_QA_lowres'
        TEST_DIR = (Path(__file__).parent / ".." / ".." / ".." / "tests" / "test_files").resolve()
        surface_filename = TEST_DIR / input_name
        ndefault = 4
        default_surf = SurfaceRZFourier.from_vmec_input(
            surface_filename, range='full torus', nphi=ndefault, ntheta=ndefault
        )
        default_surf.nfp = 1
        default_surf.stellsym = False
        
        # Initialize all the plasma boundary information
        psc_grid.plasma_boundary = kwargs.pop("plasma_boundary", default_surf)
        psc_grid.nfp = psc_grid.plasma_boundary.nfp
        psc_grid.stellsym = psc_grid.plasma_boundary.stellsym
        psc_grid.nphi = len(psc_grid.plasma_boundary.quadpoints_phi)
        psc_grid.ntheta = len(psc_grid.plasma_boundary.quadpoints_theta)
        psc_grid.plasma_unitnormals = contig(psc_grid.plasma_boundary.unitnormal().reshape(-1, 3))
        psc_grid.plasma_points = contig(psc_grid.plasma_boundary.gamma().reshape(-1, 3))
        psc_grid.symmetry = psc_grid.plasma_boundary.nfp * (psc_grid.plasma_boundary.stellsym + 1)
        if psc_grid.stellsym:
            psc_grid.stell_list = [1, -1]
        else:
            psc_grid.stell_list = [1]
        Ngrid = psc_grid.nphi * psc_grid.ntheta
        Nnorms = np.ravel(np.sqrt(np.sum(psc_grid.plasma_boundary.normal() ** 2, axis=-1)))
        psc_grid.grid_normalization = np.sqrt(Nnorms / Ngrid)
        Bn_plasma = kwargs.pop("Bn_plasma", 
                               np.zeros((psc_grid.nphi, psc_grid.ntheta))
        )
        if not np.allclose(Bn_plasma.shape, (psc_grid.nphi, psc_grid.ntheta)): 
            raise ValueError('Plasma magnetic field surface data is incorrect shape.')
        psc_grid.Bn_plasma = Bn_plasma
        psc_grid.plasma_boundary_full = kwargs.pop("plasma_boundary_full", psc_grid.plasma_boundary)
        
        # generate planar TF coils
        ncoils = 4
        R0 = 1.4
        R1 = 0.9
        order = 4
        total_current = 1e6
        base_curves = create_equally_spaced_curves(
            ncoils, psc_grid.plasma_boundary.nfp, 
            stellsym=psc_grid.plasma_boundary.stellsym, 
            R0=R0, R1=R1, order=order, numquadpoints=128
        )
        base_currents = [(Current(total_current / ncoils * 1e-5) * 1e5) for _ in range(ncoils)]
        total_current = Current(total_current)
        total_current.fix_all()
        default_coils = coils_via_symmetries(
            base_curves, base_currents, 
            psc_grid.plasma_boundary.nfp, 
            psc_grid.plasma_boundary.stellsym
        )
        # fix all the coil shapes so only the currents are optimized
        for i in range(ncoils):
            base_curves[i].fix_all()
        coils_TF = kwargs.pop("coils_TF", default_coils)
        
        # Get all the TF coils data
        psc_grid.I_TF = np.array([coil.current.get_value() for coil in coils_TF])
        psc_grid.dl_TF = np.array([coil.curve.gammadash() for coil in coils_TF])
        psc_grid.gamma_TF = np.array([coil.curve.gamma() for coil in coils_TF])
        TF_curves = np.array([coil.curve for coil in coils_TF])
        curves_to_vtk(TF_curves, psc_grid.out_dir + "coils_TF" + str(psc_grid.plasma_boundary), close=True, scalar_data=psc_grid.I_TF)
        
        # Setup the B field from the TF coils
        # Many big calls to B_TF.B() so can make an interpolated object
        # if accuracy is not paramount
        interpolated_field = kwargs.pop("interpolated_field", False)
        B_TF = BiotSavart(coils_TF)
        if interpolated_field:
            # Need interpolated region big enough to cover all the coils and the plasma
            n = 20
            degree = 2
            rs = np.linalg.norm(psc_grid.grid_xyz[:, :2] + R ** 2, axis=-1)
            zs = psc_grid.grid_xyz[:, 2]
            rrange = (0, np.max(rs) + R, n)  # need to also cover the plasma
            if psc_grid.nfp > 1:
                phirange = (0, 2*np.pi/psc_grid.nfp, n*2)
            else:
                phirange = (0, 2 * np.pi, n * 2)
            if psc_grid.stellsym:
                zrange = (0, np.max(zs) + R, n // 2)
            else:
                zrange = (np.min(zs) - R, np.max(zs) + R, n // 2)
            psc_grid.B_TF = InterpolatedField(
                B_TF, degree, rrange, phirange, zrange, 
                True, nfp=psc_grid.nfp, stellsym=psc_grid.stellsym
            )
        B_TF.set_points(psc_grid.grid_xyz)
        B_axis = calculate_on_axis_B(B_TF, psc_grid.plasma_boundary, print_out=False)
        # Normalization of the ||A*Linv*psi - b||^2 objective 
        # representing Bnormal errors on the plasma surface
        psc_grid.normalization = B_axis ** 2 * psc_grid.plasma_boundary.area()
        psc_grid.fac2_norm = psc_grid.fac ** 2 / psc_grid.normalization
        psc_grid.B_TF = B_TF
        
        # Order of the coils. For unit tests, needs > 400
        psc_grid.ppp = kwargs.pop("ppp", 1000)

        psc_grid.coil_normals = np.array(
            [np.cos(psc_grid.alphas) * np.sin(psc_grid.deltas),
              -np.sin(psc_grid.alphas),
              np.cos(psc_grid.alphas) * np.cos(psc_grid.deltas)]
        ).T
        # deal with -0 terms in the normals, which screw up the arctan2 calculations
        psc_grid.coil_normals[
            np.logical_and(np.isclose(psc_grid.coil_normals, 0.0), 
                           np.copysign(1.0, psc_grid.coil_normals) < 0)
            ] *= -1.0
        
        # Initialize curve objects corresponding to each PSC coil for 
        # plotting in 3D
        psc_grid.setup_full_grid()
        psc_grid.setup_orientations(psc_grid.alphas, psc_grid.deltas)
        psc_grid.update_psi()
        # psc_grid.L_inv = inv(psc_grid.L, check_finite=False)
        # psc_grid.I = -psc_grid.L_inv[:psc_grid.num_psc, :psc_grid.num_psc] @ psc_grid.psi / psc_grid.fac
        psc_grid.setup_currents_and_fields()
        
        # Initialize CurvePlanarFourier objects for the PSCs, mostly for
        # plotting purposes
        psc_grid.setup_curves()
        psc_grid.plot_curves()
        
        # Set up vector b appearing in objective ||A*Linv*psi - b||^2
        # since it only needs to be initialized once and then stays fixed.
        psc_grid.b_vector()
        
        # Initialize kappas = [alphas, deltas] which is the array of
        # optimization variables used in this work. 
        kappas = np.ravel(np.array([psc_grid.alphas, psc_grid.deltas]))
        psc_grid.kappas = kappas
        return psc_grid
    
    def setup_currents_and_fields(self):
        """ 
        Calculate the currents and fields from all the PSCs. Note that L 
        will be well-conditioned unless you accidentally initialized
        the PSC coils touching/intersecting, in which case it will be
        VERY ill-conditioned! 
        """
        
        # t1 = time.time()
        # print(np.max(np.abs(I_test/self.I)), np.argmax(np.abs(I_test/self.I)))
        # print(I_test[np.argmax(np.abs(I_test/self.I))])
        # can fail from numerical error accumulation on low currents PSC 
        # assert np.allclose(self.I, I_test, rtol=1e-2)
        # t2 = time.time()
        # print('Linv time = ', t2 - t1)
        # t1 = time.time()
        self.L_inv = np.linalg.inv(self.L)
        # self.L_inv = np.linalg.pinv(self.L, rcond=1e-5, hermitian=True)
        # t2 = time.time()
        # print('inv time = ', t2-t1)

        # self.I = -self.L_inv[:self.num_psc, :self.num_psc] @ self.psi / self.fac
        self.I_all = -self.L_inv @ self.psi_total / self.fac
        self.I = self.I_all[:self.num_psc]
        # print(self.L[:self.num_psc, :self.num_psc] @ self.I, self.psi / self.fac)
        # print(self.L @ self.I_all, self.psi_total / self.fac)
        # print('I_all = ', self.I_all)
        # print('L_all = ', self.L)
        # print('max flux in each PSC = ', np.max(np.abs(self.L @ self.I_all + self.psi_total / self.fac)))
        self.setup_A_matrix()
        self.Bn_PSC = (self.A_matrix @ self.I).reshape(-1)   # * self.fac
        self.Bn_PSC_full = (self.A_matrix_full @ self.I_all).reshape(-1)
        # print('Bn = ', self.Bn_PSC)
        
    def setup_A_matrix(self):
        """
        """
        # A_matrix has shape (num_plasma_points, num_coils)
        A_matrix = np.zeros((self.nphi * self.ntheta, self.num_psc))
        # A_matrix_full = np.zeros((self.nphi * self.ntheta * self.symmetry, self.num_psc * self.symmetry))
        # Need to rotate and flip it
        nn = self.num_psc
        q = 0
        for fp in range(self.nfp):
            for stell in self.stell_list:
                A_matrix += sopp.A_matrix_simd(
                    contig(self.grid_xyz_all[q * nn: (q + 1) * nn, :]),
                    self.plasma_points,
                    contig(self.alphas_total[q * nn: (q + 1) * nn]),
                    contig(self.deltas_total[q * nn: (q + 1) * nn]),
                    self.plasma_unitnormals,
                    self.R,
                ) # * stell * (-1) ** fp # accounts for sign change of the currents (does it?)
                q = q + 1
                
        # grid_xyz_all is definitely right, alphas_total/deltas_total may be suss,
        self.A_matrix_full = 2.0 * sopp.A_matrix_simd(
            contig(self.grid_xyz_all),
            contig(self.plasma_boundary_full.gamma().reshape(-1, 3)),
            contig(self.alphas_total),
            contig(self.deltas_total),
            contig(self.plasma_boundary_full.unitnormal().reshape(-1, 3)),
            self.R,
        )
        # print('A_full = ', self.A_matrix_full[0, :])
        # q = 0
        # for fp in range(self.nfp):
        #     for stell in self.stell_list:
        #         phi0 = (2 * np.pi / self.nfp) * fp 
        #         self.A_matrix_full[:, q*self.num_psc:(q+1)*self.num_psc] *= stell  #* (-1.0) ** fp
        #         q += 1
        self.A_matrix = 2 * A_matrix  
        
        
        ######## Things that appear to be correct:
        # 1. psi does not need factors for discrete symmetries. That means the PSC currents don't flip either
        # 2. A_matrix is correctly symmetrized WITHOUT multiplying it by any factors since
        #    A_matrix @ I agrees with A_matrix_full @ I_all. Moreover, A_matrix_full @ I_all
        #    produces correctly symmetrized Paraview plots as far as I can tell. 
        
        # Note below line fails is plasma_boundary_full is passed as kwarg and != plasma_boundary
        # print((self.A_matrix @ self.I) / ( self.A_matrix_full[:len(self.plasma_points), :] @ self.I_all))
        # assert np.allclose(self.A_matrix @ self.I, self.A_matrix_full[:len(self.plasma_points), :] @ self.I_all, rtol=1e-2)
    
    def setup_curves(self):
        """ 
        Convert the (alphas, deltas) angles into the actual circular coils
        that they represent. Also generates the symmetrized coils.
        """
        from . import CurvePlanarFourier
        from simsopt.field import apply_symmetries_to_curves

        order = 1
        ncoils = self.num_psc
        curves = [CurvePlanarFourier(order*self.ppp, order, nfp=1, stellsym=False) for i in range(ncoils)]
        for ic in range(ncoils):
            alpha2 = self.alphas[ic] / 2.0
            delta2 = self.deltas[ic] / 2.0
            calpha2 = np.cos(alpha2)
            salpha2 = np.sin(alpha2)
            cdelta2 = np.cos(delta2)
            sdelta2 = np.sin(delta2)
            dofs = np.zeros(10)
            dofs[0] = self.R
            # dofs[1] = 0.0
            # dofs[2] = 0.0
            # Conversion from Euler angles in 3-2-1 body sequence to 
            # quaternions: 
            # https://en.wikipedia.org/wiki/Conversion_between_quaternions_and_Euler_angles
            dofs[3] = calpha2 * cdelta2
            dofs[4] = salpha2 * cdelta2
            dofs[5] = calpha2 * sdelta2
            dofs[6] = -salpha2 * sdelta2
            # Now specify the center 
            dofs[7:10] = self.grid_xyz[ic, :]
            curves[ic].set_dofs(dofs)
        self.curves = curves
        self.all_curves = apply_symmetries_to_curves(curves, self.nfp, self.stellsym)

    def plot_curves(self, filename=''):
        """
        Plots the unique PSC coil curves in real space and plots the normal 
        vectors, fluxes, currents, and TF fields at the center of coils. 
        Repeats the plotting for all of the PSC coil curves after discrete
        symmetries are applied. If the TF field is not type InterpolatedField,
        less quantities are plotted because of computational time. 

        Parameters
        ----------
        filename : string
            File name extension for some of the saved files.

        """
        from pyevtk.hl import pointsToVTK
        from . import curves_to_vtk
        from simsopt.field import InterpolatedField, Current, coils_via_symmetries
        
        curves_to_vtk(self.curves, self.out_dir + "psc_curves", close=True, scalar_data=self.I)
        if isinstance(self.B_TF, InterpolatedField):
            self.B_TF.set_points(self.grid_xyz)
            B = self.B_TF.B()
            pointsToVTK(self.out_dir + 'curve_centers', 
                        contig(self.grid_xyz[:, 0]),
                        contig(self.grid_xyz[:, 1]), 
                        contig(self.grid_xyz[:, 2]),
                        data={"n": (contig(self.coil_normals[:, 0]), 
                                    contig(self.coil_normals[:, 1]),
                                    contig(self.coil_normals[:, 2])),
                              "psi": contig(self.psi),
                              "I": contig(self.I),
                              "B_TF": (contig(B[:, 0]), 
                                        contig(B[:, 1]),
                                        contig(B[:, 2])),
                              },
            )
        else:
            pointsToVTK(self.out_dir + 'curve_centers', 
                        contig(self.grid_xyz[:, 0]),
                        contig(self.grid_xyz[:, 1]), 
                        contig(self.grid_xyz[:, 2]),
                        data={"n": (contig(self.coil_normals[:, 0]), 
                                    contig(self.coil_normals[:, 1]),
                                    contig(self.coil_normals[:, 2])),
                              },
            )
        curves_to_vtk(self.all_curves, self.out_dir + filename + "all_psc_curves", close=True, scalar_data=self.I_all)
        # if isinstance(self.B_TF, InterpolatedField):
        self.B_TF.set_points(self.grid_xyz_all)
        B = self.B_TF.B()
        pointsToVTK(self.out_dir + filename + 'all_curve_centers', 
                    contig(self.grid_xyz_all[:, 0]),
                    contig(self.grid_xyz_all[:, 1]), 
                    contig(self.grid_xyz_all[:, 2]),
                    data={"n": (contig(self.coil_normals_all[:, 0]), 
                                contig(self.coil_normals_all[:, 1]),
                                contig(self.coil_normals_all[:, 2])),
                          "psi": contig(self.psi_total),
                          "I": contig(self.I_all),
                          "B_TF": (contig(B[:, 0]), 
                                   contig(B[:, 1]),
                                   contig(B[:, 2])),
                          "-LI(=psi)": contig(-self.L @ self.I_all * self.fac),
                          },
        )
        # else:
        #     pointsToVTK(self.out_dir + filename + 'all_curve_centers', 
        #                 contig(self.grid_xyz_all[:, 0]),
        #                 contig(self.grid_xyz_all[:, 1]), 
        #                 contig(self.grid_xyz_all[:, 2]),
        #                 data={"n": (contig(self.coil_normals_all[:, 0]), 
        #                             contig(self.coil_normals_all[:, 1]),
        #                             contig(self.coil_normals_all[:, 2])),
        #                       },
        #     )
        
    def b_vector(self):
        """
        Initialize the vector b appearing in the ||A*Linv*psi - b||^2
        objective term representing Bnormal errors on the plasma surface.

        """
        Bn_plasma = self.Bn_plasma.reshape(-1)
        self.B_TF.set_points(self.plasma_points)
        Bn_TF = np.sum(
            self.B_TF.B().reshape(-1, 3) * self.plasma_unitnormals, axis=-1
        )
        self.b_opt = (Bn_TF + Bn_plasma) / self.fac
        
    def least_squares(self, kappas, verbose=False):
        """
        Evaluate the 0.5 * ||A*Linv*psi - b||^2
        objective term representing Bnormal errors on the plasma surface.
        
        Parameters
        ----------
        kappas : 1D numpy array, shape 2N
            Array of [alpha_1, ..., alpha_N, delta_1, ..., delta_N] that
            represents the coil orientation degrees of freedom being used
            for optimization.
        verbose : bool
            Flag to print out objective values and current array of kappas.
            
        Returns
        -------
            BdotN2: double
                The value of 0.5 * ||A*Linv*psi - b||^2 evaluated with the
                array of kappas.

        """
        # t1 = time.time()
        alphas = kappas[:len(kappas) // 2]
        deltas = kappas[len(kappas) // 2:]
        self.setup_orientations(alphas, deltas)
        self.update_psi()
        self.setup_currents_and_fields()
        Ax_b = (self.Bn_PSC + self.b_opt) * self.grid_normalization
        # print(self.Bn_PSC, self.I, self.)
        # print(self.Bn_PSC, self.b_opt, self.I[0], self.psi[0], self.L_inv[0, 1])
        BdotN2 = 0.5 * Ax_b.T @ Ax_b * self.fac2_norm
        # t2 = time.time()
        # print('setup currents time = ', t2 - t1)
        # outstr = f"Normalized f_B = {BdotN2:.3e} "
        # for i in range(len(kappas)):
        #     outstr += f"kappas[{i:d}] = {kappas[i]:.2e} "
        # outstr += "\n"
        # if verbose:
        #     print(outstr)
        self.BdotN2_list.append(BdotN2)
        return BdotN2
    
    def least_squares_jacobian(self, kappas, verbose=False):
        """
        Compute Jacobian of the ||A*Linv*psi - b||^2
        objective term representing Bnormal errors on the plasma surface,
        for using BFGS in scipy.minimize.
        
        Parameters
        ----------
        kappas : 1D numpy array, shape 2N
            Array of [alpha_1, ..., alpha_N, delta_1, ..., delta_N] that
            represents the coil orientation degrees of freedom being used
            for optimization.
        Returns
        -------
            jac: 1D numpy array, shape 2N
                The gradient values of 0.5 * ||A*Linv*psi - b||^2 evaluated 
                with the array of kappas.

        """
        t1_fin = time.time()
        alphas = kappas[:len(kappas) // 2]
        deltas = kappas[len(kappas) // 2:]
        # print('alphas_old, deltas_old = ', self.alphas, self.deltas)
        # print(alphas, deltas)
        # t1 = time.time()
        self.setup_orientations(alphas, deltas)
        self.update_psi()
        self.setup_currents_and_fields()
        # t2 = time.time()
        # print('Setup time = ', t2 - t1)
        # Two factors of grid normalization since it is not added to the gradients
        t1 = time.time()
        Ax_b = (self.Bn_PSC + self.b_opt) * self.grid_normalization
        A_deriv = self.A_deriv() 
        # I = -Linv @ self.psi
        # print('Linv = ', Linv[0, 2])
        # print('psi = ', self.psi[0])
        # print('I = ', self.I[0])
        # print('Bn = ', self.Bn_PSC[0], self.b_opt[0])
        grad_alpha1 = A_deriv[:, :self.num_psc] * self.I
        grad_delta1 = A_deriv[:, self.num_psc:] * self.I
        # print(A_deriv, self.A_matrix, Lpsi, Linv, self.psi)
        # Should be shape (num_plasma_points, 2 * num_psc)
        grad_kappa1 = self.grid_normalization[:, None] * np.hstack((grad_alpha1, grad_delta1)) 
        t2 = time.time()
        # print('grad_A1 time = ', t2 - t1)
        
        # Analytic calculation too expensive
        L_deriv = self.L_deriv()  # / self.fac
        # L_deriv = (self.L - self.L_prev) / (kappas - self.kappas_prev)
        # t1 = time.time()
        Linv2 = -(self.L_inv ** 2 @ self.psi_total)  # @ self.L_inv
        # Linv2 = self.L_inv @ self.L_inv @ self.psi_total
        ncoil_sym = L_deriv.shape[1]
        I_deriv1 = L_deriv[:self.num_psc, :self.num_psc, :] @ Linv2
        I_deriv2 = L_deriv[ncoil_sym:ncoil_sym + self.num_psc, :self.num_psc, :] @ Linv2
        I_deriv = np.hstack((I_deriv1, I_deriv2))
        
        # Linv = self.L_inv
        # L_deriv = self.L_deriv()
        # Linv_deriv = -L_deriv @ Linv @ Linv
        # for i in range(self.num_psc * 2):
        #     Linv_deriv[i, :, :] = (Linv_deriv[i, :, :] + Linv_deriv[i, :, :].T) / 2.0
        # ncoil_sym = L_deriv.shape[1]
        # I_deriv1 = -(Linv_deriv @ self.psi_total)[:self.num_psc, :self.num_psc]   # / self.fac ** 2   # ** 2
        # I_deriv2 = -(Linv_deriv @ self.psi_total)[ncoil_sym:ncoil_sym + self.num_psc, :self.num_psc]   # / self.fac ** 2   # ** 2
        # I_deriv = np.hstack((I_deriv1, I_deriv2))
        
        # # print(Linv_deriv, Linv, L_deriv, Linv_deriv.shape, L_deriv.shape, np.max(np.max(np.abs(Linv_deriv), axis=-1), axis=-1))
        # # exit()
        # # print('Linv = ', Linv, L_deriv)
        grad_kappa2 = self.grid_normalization[:, None] * (self.A_matrix @ I_deriv)  #/ self.fac
        # t2 = time.time()
        # print('Linv_deriv time = ', t2 - t1)
        # t1 = time.time()
        psi_deriv = self.psi_deriv()  # / self.fac
        Linv = self.L_inv[:self.num_psc, :self.num_psc]

        # Should be * or @ below for Linv with psi_deriv?
        # Really should be psi_deriv is shape(num_psc, 2 * num_psc) and then @
        # but dpsi_dalpha_i gives a delta function on the coil flux that corresponds
        # to coil i
        # print(psi_deriv.shape)
        I_deriv2 = -Linv * psi_deriv[:self.num_psc]   # / self.fac   # ** 2
        I_deriv3 = -Linv * psi_deriv[self.num_psc:]    # / self.fac   # ** 2
        grad_alpha3 = self.A_matrix @ I_deriv2  # / self.fac ** 2
        grad_delta3 = self.A_matrix @ I_deriv3  #/ self.fac ** 2
        grad_kappa3 = self.grid_normalization[:, None] * np.hstack((grad_alpha3, grad_delta3))
        # t2 = time.time()
        # print('grad_psi time = ', t2 - t1)
        # missing mu0 fac
        # print(grad_kappa1.shape, grad_kappa2.shape, grad_kappa3.shape)
        # grad = grad_kappa1 + grad_kappa2 + grad_kappa3
        # print(self.Bn_PSC, self.b_opt)
        # print('grads = ',  (Ax_b.T @ grad_kappa1) * self.fac2_norm,  
        #       Ax_b.T @ grad_kappa2* self.fac2_norm,
        #       Ax_b.T @ grad_kappa3* self.fac2_norm)
        # t2 = time.time()
        # print('grad_A3 time = ', t2 - t1, grad_alpha3.shape, grad_kappa3.shape)
        # if verbose:
            # print('Jacobian = ', Ax_b @ (grad_kappa1 + grad_kappa2 + grad_kappa3))
        # minus sign in front because I = -Linv @ psi
        jac = (Ax_b.T @ (grad_kappa1 + grad_kappa2 + grad_kappa3)) * self.fac2_norm 
        # print(jac * self.fac2_norm)
        # print('exact jac = ', jac * self.fac2_norm)
        # exit()
        t2_fin = time.time()
        # print('Total time = ', t2_fin - t1_fin)
        return jac
    
    def A_deriv(self):
        """
        Should return gradient of the A matrix evaluated at each point on the 
        plasma boundary. This is a wrapper function for a faster c++ call.
        
        Returns
        -------
            A_deriv: 2D numpy array, shape (num_plasma_points, 2 * num_wp) 
                The gradient of the A matrix evaluated on all the plasma 
                points, with respect to the WP angles alpha_i and delta_i. 
        """
        nn = self.num_psc
        dA_dkappa = np.zeros((len(self.plasma_points), 2 * self.num_psc))
        q = 0
        for fp in range(self.nfp):
            for stell in self.stell_list:
                dA = sopp.dA_dkappa_simd(
                    contig(self.grid_xyz_all[q * nn: (q + 1) * nn, :]),
                    self.plasma_points,
                    contig(self.alphas_total[q * nn: (q + 1) * nn]),
                    contig(self.deltas_total[q * nn: (q + 1) * nn]),
                    self.plasma_unitnormals,
                    self.quad_points_phi,
                    self.quad_weights,
                    self.R,
                )
                # fac_alpha = np.cos(self.alphas + self.alphas_total[q * nn: (q + 1) * nn])
                # print(q, fp, stell, self.alphas_total[q * nn: (q + 1) * nn], self.alphas, 
                #       self.alphas + self.alphas_total[q * nn: (q + 1) * nn], fac_alpha)
                dA_dkappa[:, :self.num_psc] += dA[:, :self.num_psc]  #* self.alphas_total[q * nn: (q + 1) * nn] / self.alphas #* (-1) ** fp
                dA_dkappa[:, self.num_psc:] += dA[:, self.num_psc:]  #* self.deltas_total[q * nn: (q + 1) * nn] / self.deltas
                # print(fp, stell, dA[0, 0], self.grid_xyz_all[q * nn: q * nn + 1, :])
                # print(self.plasma_points[0, :], 
                #       self.alphas_total[q * nn: q * nn + 1],
                #       self.deltas_total[q * nn: q * nn + 1],
                #       self.plasma_unitnormals[0, :],
                #       )
                q = q + 1
        dA = sopp.dA_dkappa_simd(
            contig(self.grid_xyz_all),
            self.plasma_points,
            contig(self.alphas_total),
            contig(self.deltas_total),
            self.plasma_unitnormals,
            self.quad_points_phi,
            self.quad_weights,
            self.R,
        )
        print('dA = ', dA[0, :])
        return dA * np.pi # dA_dkappa * np.pi # rescale by pi for gauss-leg quadrature
    
    def L_deriv(self):
        """
        Should return gradient of the inductance matrix L that satisfies 
        L^(-1) * Psi = I for the PSC arrays.
        Returns
        -------
            grad_L: 3D numpy array, shape (2 * num_psc, num_psc, num_plasma_points) 
                The gradient of the L matrix with respect to the PSC angles
                alpha_i and delta_i. 
        """
        # nn = self.num_psc
        # L_deriv = np.zeros((2 * self.num_psc, self.num_psc, self.num_psc))
        # q = 0
        # for fp in range(self.nfp):
        #     for stell in self.stell_list:
        t1 = time.time()
        L_deriv = sopp.L_deriv_simd(
            self.grid_xyz_all_normalized, 
            self.alphas_total,
            self.deltas_total,
            self.quad_points_phi,
            self.quad_weights,
            self.num_psc,
            self.stellsym,
            self.nfp
        )  # because L_deriv_simd adds the transpose already
        t2 = time.time()
        # print('L_deriv call = ', t2 - t1)
        # t1 = time.time()
        nsym = self.symmetry
        # # L_deriv_copy = np.zeros(L_deriv.shape)
        
        # Extra work required to impose the symmetries correctly -- only works for nfp == 2
        if nsym > 1:
            ncoils_sym = self.num_psc * nsym
            # for i in range(self.num_psc):
            q = 1
            for fp in range(self.nfp):
                for stell in self.stell_list:
                    if (fp > 0) or stell != 1:
                        L_deriv[:self.num_psc, :, :] += L_deriv[q * self.num_psc:(q + 1) * self.num_psc, :, :] * (-1) ** fp
                        L_deriv[ncoils_sym:ncoils_sym + self.num_psc, :, :] += L_deriv[ncoils_sym + q * self.num_psc:(q + 1) * self.num_psc + ncoils_sym, :, :] * (-1) ** fp * stell
                        q = q + 1
        # else:
        #     L_deriv_copy = L_deriv
            
        # symmetrize it
        L_deriv = (L_deriv + np.transpose(L_deriv, axes=[0, 2, 1]))
        # t2 = time.time()
        # print('L_deriv reshape = ', t2 - t1)
        # if keeping track of the number of coil turns, need factor
        # of Nt ** 2 below as well
        return L_deriv * self.R  # * self.fac
    
    # def coil_forces(self):
    #     """
    #     """
    #     rho = self.R * np.ones(1)
    #     coils_grid_orig = sopp.flux_xyz(
    #         self.grid_xyz_all, 
    #         self.alphas_total,
    #         self.deltas_total, 
    #         contig(rho), 
    #         self.quad_points_phi, 
    #     )
    #     coils_grid = coils_grid_orig.reshape(-1, 3)
    #     self.B_TF.set_points(contig(coils_grid))
    #     B = self.B_TF.B().reshape(-1, 3)
    #     F_TF = sopp.coil_forces(
    #         self.grid_xyz_all,
    #         contig(B),
    #         self.alphas_total, 
    #         self.deltas_total,
    #         self.quad_points_phi,
    #         self.quad_weights,
    #     )
        
    #     # Need to remove the ith coil and then sum over j != i
    #     coils_grid = coils_grid_orig.reshape(self.num_psc, -1, 3)
    #     A_matrix = np.zeros((self.num_psc, self.num_psc, coils_grid.shape[1], 3))
    #     nn = self.num_psc
    #     q = 0
    #     for fp in range(self.nfp):
    #         for stell in self.stell_list:
    #             A_matrix += sopp.coil_forces_A_matrix(
    #                 contig(self.grid_xyz_all[q * nn: (q + 1) * nn, :]),
    #                 contig(coils_grid),
    #                 contig(self.alphas_total[q * nn: (q + 1) * nn]),
    #                 contig(self.deltas_total[q * nn: (q + 1) * nn]),
    #                 self.R,
    #             )  # accounts for sign change of the currents (does it?)
    #             q = q + 1
    #     self.A_matrix = 2 * A_matrix  
    #     F_PSC = sopp.coil_forces_matrix(
    #         contig(self.grid_xyz),
    #         contig(self.A_matrix),
    #         contig(self.alphas), 
    #         contig(self.deltas),  # total here?
    #         self.quad_points_phi,
    #         self.quad_weights,
    #     )
    #     return F_TF, F_PSC
    
    def psi_deriv(self):
        """
        Should return gradient of the inductance matrix L that satisfies 
        L^(-1) * Psi = I for the PSC arrays.
        Returns
        -------
            grad_psi: 1D numpy array, shape (2 * num_psc) 
                The gradient of the psi vector with respect to the PSC angles
                alpha_i and delta_i. 
        """
        nn = self.num_psc
        psi_deriv = np.zeros(2 * self.num_psc)
        q = 0
        # for fp in range(self.nfp):
        #     for stell in self.stell_list:
        psi_deriv = sopp.dpsi_dkappa(
            contig(self.I_TF),
            contig(self.dl_TF),
            contig(self.gamma_TF),
            contig(self.grid_xyz_all),  #_all[q * nn: (q + 1) * nn, :]),
            contig(self.alphas_total),  # _total[q * nn: (q + 1) * nn]),
            contig(self.deltas_total),  #_total[q * nn: (q + 1) * nn]),
            contig(self.coil_normals_all),  # _all[q * nn: (q + 1) * nn, :]),
            contig(self.quad_points_rho),
            contig(self.quad_points_phi),
            self.quad_weights,
            self.R,
        )
        
        # dpsi/dkappa depends implicity on how kappa got changed! 
        nsym = self.symmetry
        if nsym > 1:
            ncoils_sym = self.num_psc * nsym
            for fp in range(self.nfp):
                # phi0 = (2 * np.pi / self.nfp) * fp
                for stell in self.stell_list:
                    psi_deriv[q * self.num_psc:(q + 1) * self.num_psc] *= self.alphas_total[q * self.num_psc:(q + 1) * self.num_psc] / self.alphas
                    psi_deriv[q * self.num_psc + ncoils_sym:ncoils_sym + (q + 1) * self.num_psc] *= self.deltas_total[q * self.num_psc:(q + 1) * self.num_psc] / self.deltas
                    q = q + 1
        # print('psi_deriv = ', psi_deriv)
        return psi_deriv * (1.0 / self.gamma_TF.shape[1])
    
    def setup_orientations(self, alphas, deltas):
        """
        Each time that optimization changes the PSC angles, need to update
        all the fields, currents, and fluxes to be consistent with the 
        new angles. This function does this updating to get the entire class
        object the new angle values. 
        
        Args
        ----------
        alphas : 1D numpy array, shape (num_pscs)
            Rotation angles of every PSC around the x-axis.
        deltas : 1D numpy array, shape (num_pscs)
            Rotation angles of every PSC around the y-axis.
        Returns
        -------
            grad_Psi: 3D numpy array, shape (2 * num_psc, num_psc) 
                The gradient of the Psi vector with respect to the PSC angles
                alpha_i and delta_i. 
        """
        
        # Need to check is alphas and deltas are in [-pi, pi] and remap if not
        self.alphas = alphas
        self.deltas = deltas
        
        self.coil_normals = np.array(
            [np.cos(alphas) * np.sin(deltas),
              -np.sin(alphas),
              np.cos(alphas) * np.cos(deltas)]
        ).T
        # deal with -0 terms in the normals, which screw up the arctan2 calculations
        self.coil_normals[
            np.logical_and(np.isclose(self.coil_normals, 0.0), 
                           np.copysign(1.0, self.coil_normals) < 0)
            ] *= -1.0
                
        # Apply discrete symmetries to the alphas and deltas and coordinates
        self.update_alphas_deltas()
        
        # self.update_psi()

        # Recompute the inductance matrices with the newly rotated coils
        L_total = sopp.L_matrix(
            self.grid_xyz_all_normalized, 
            self.alphas_total, 
            self.deltas_total,
            self.quad_points_phi,
            self.quad_weights
        ) # * self.quad_dphi ** 2 / (np.pi ** 2)
        t2 = time.time()
        # print('L calculation time = ', t2 - t1)
        L_total = (L_total + L_total.T)
        # Add in self-inductances
        np.fill_diagonal(L_total, (np.log(8.0 * self.R / self.a) - 2.0) * 4 * np.pi)
        # rescale inductance
        # if keeping track of the number of coil turns, need factor
        # of Nt ** 2 below as well
        self.L = L_total * self.R  #* self.fac
        
        # Calculate the PSC B fields
        # t1 = time.time()
        # t2 = time.time()
        # print('Setup fields time = ', t2 - t1)
        
    def update_psi(self):
        # Update the flux grid with the new normal vectors
        flux_grid = sopp.flux_xyz(
            contig(self.grid_xyz_all), 
            contig(self.alphas_total),
            contig(self.deltas_total), 
            contig(self.quad_points_rho), 
            contig(self.quad_points_phi), 
        )
        self.flux_grid = np.array(flux_grid).reshape(-1, 3)
        self.B_TF.set_points(contig(self.flux_grid))

        # t1 = time.time()
        
        # Only works if using the TF field as expected,
        # i.e. not doing the unit test with two coils computing the flux from each other
        # self.B_TF2 = sopp.B_TF(
        #     contig(self.I_TF),
        #     contig(self.dl_TF),
        #     contig(self.gamma_TF),
        #     contig(np.array(flux_grid).reshape(-1, 3)),
        #     ) * (1.0 / self.gamma_TF.shape[1])
        # assert np.allclose(self.B_TF.B(), B_TF2, rtol=1e-2)
        N = len(self.quad_points_rho)
        # # Update the flux values through the newly rotated coils
        # t1 = time.time()
        self.psi_total = sopp.flux_integration(
            contig(self.B_TF.B().reshape(len(self.alphas_total), N, N, 3)),
            contig(self.quad_points_rho),
            contig(self.coil_normals_all),
            self.quad_weights
        )
        self.psi = self.psi_total[:self.num_psc]
        
        # Looks like don't flip psi is correct! (tested on QH)
        # self.psi_total = np.zeros(self.num_psc * self.symmetry)
        # q = 0
        # for fp in range(self.nfp):
        #     for stell in self.stell_list:
        #         self.psi_total[q * self.num_psc:(q + 1) * self.num_psc] *= stell  #* (-1) ** fp
        #         q = q + 1
        # print('psi_total = ', self.psi_total)
        # t2 = time.time()
        # print('psi integration time = ', t2 - t1)
        # print('B_TF at flux grid = ', self.B_TF.B().reshape(len(alphas), N, N, 3))

        # self.psi *= self.dphi * self.drho   # * 100
        # t2 = time.time()
        # print('Flux2 time = ', t2 - t1)
        # t1 = time.time()        
        
    def setup_full_grid(self):
        """
        Initialize the field-period and stellarator symmetrized grid locations
        and normal vectors for all the PSC coils. Note that when alpha_i
        and delta_i angles change, coil_i location does not change, only
        its normal vector changes. 
        """
        nn = self.num_psc
        self.grid_xyz_all = np.zeros((nn * self.symmetry, 3))
        q = 0
        for fp in range(self.nfp):
            for stell in self.stell_list:
                phi0 = (2 * np.pi / self.nfp) * fp
                # get new locations by flipping the y and z components, then rotating by phi0
                self.grid_xyz_all[nn * q: nn * (q + 1), 0] = self.grid_xyz[:, 0] * np.cos(phi0) - self.grid_xyz[:, 1] * np.sin(phi0) * stell
                self.grid_xyz_all[nn * q: nn * (q + 1), 1] = self.grid_xyz[:, 0] * np.sin(phi0) + self.grid_xyz[:, 1] * np.cos(phi0) * stell
                self.grid_xyz_all[nn * q: nn * (q + 1), 2] = self.grid_xyz[:, 2] * stell
                q += 1
                
        self.grid_xyz_all = contig(self.grid_xyz_all)
        self.grid_xyz_all_normalized = self.grid_xyz_all / self.R
        self.update_alphas_deltas()
    
    def update_alphas_deltas(self):
        """
        Initialize the field-period and stellarator symmetrized normal vectors
        for all the PSC coils. This is required whenever the alpha_i
        and delta_i angles change.
        """
        nn = self.num_psc
        self.alphas_total = np.zeros(nn * self.symmetry)
        self.deltas_total = np.zeros(nn * self.symmetry)
        self.coil_normals_all = np.zeros((nn * self.symmetry, 3))
        q = 0
        for fp in range(self.nfp):
            for stell in self.stell_list:
                phi0 = (2 * np.pi / self.nfp) * fp
                # get new normal vectors by flipping the x component, then rotating by phi0
                self.coil_normals_all[nn * q: nn * (q + 1), 0] = self.coil_normals[:, 0] * np.cos(phi0) * stell - self.coil_normals[:, 1] * np.sin(phi0) 
                self.coil_normals_all[nn * q: nn * (q + 1), 1] = self.coil_normals[:, 0] * np.sin(phi0) * stell + self.coil_normals[:, 1] * np.cos(phi0) 
                self.coil_normals_all[nn * q: nn * (q + 1), 2] = self.coil_normals[:, 2]
                # normals = self.coil_normals_all[q * nn: (q + 1) * nn, :]
                # # normals = self.coil_normals
                # # get new deltas by flipping sign of deltas, then rotation alpha by phi0
                # deltas = np.arctan2(normals[:, 0], normals[:, 2]) # + np.pi
                
                ####### Add back in lines once done debugging on QH ##########################
                # deltas[abs(deltas) == np.pi] = 0.0
                # if fp == 0 and stell == 1:
                #     shift_deltas = self.deltas - deltas  # +- pi
                # deltas += shift_deltas
                # alphas = -np.arctan2(normals[:, 1] * np.cos(deltas), normals[:, 2])
                # alphas[abs(alphas) == np.pi] = 0.0
                # if fp == 0 and stell == 1:
                #     shift_alphas = self.alphas - alphas  # +- pi
                # alphas += shift_alphas
                # print(q, fp, stell, alphas, deltas)
                # self.alphas_total[nn * q: nn * (q + 1)] = alphas
                # self.deltas_total[nn * q: nn * (q + 1)] = deltas
                # alphas2 = -np.arcsin(np.sin(phi0) * stell * np.cos(
                #     self.alphas) * np.sin(self.deltas) - np.cos(phi0) * np.sin(self.alphas))  #(self.alphas + phi0)  # * stell
                deltas = np.arctan2(np.cos(phi0) * stell * np.cos(
                    self.alphas) * np.sin(self.deltas) + np.sin(phi0) * np.sin(self.alphas),
                    np.cos(self.alphas) * np.cos(self.deltas))
                deltas[abs(deltas) == np.pi] = 0.0
                if fp == 0 and stell == 1:
                    shift_deltas = self.deltas - deltas  # +- pi
                deltas += shift_deltas
                alphas = -np.arctan2(np.cos(deltas) * (np.sin(phi0) * stell * np.cos(
                    self.alphas) * np.sin(self.deltas) - np.cos(phi0) * np.sin(self.alphas)),
                    np.cos(self.alphas) * np.cos(self.deltas))
                alphas[abs(alphas) == np.pi] = 0.0
                if fp == 0 and stell == 1:
                    shift_alphas = self.alphas - alphas  # +- pi
                alphas += shift_alphas
                # print(q, fp, stell, alphas, deltas)
                self.alphas_total[nn * q: nn * (q + 1)] = alphas
                self.deltas_total[nn * q: nn * (q + 1)] = deltas
                q = q + 1
        self.alphas_total = contig(self.alphas_total)
        self.deltas_total = contig(self.deltas_total)