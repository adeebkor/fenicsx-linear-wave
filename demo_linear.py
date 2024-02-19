#
# Linear wave
# - Heterogenous media
# =================================
# Copyright (C) 2024 Adeeb Arif Kor

import numpy as np
from mpi4py import MPI

import basix
import basix.ufl
from dolfinx import cpp
from dolfinx.fem import functionspace, Function
from dolfinx.io import XDMFFile, VTXWriter
from dolfinx.mesh import locate_entities_boundary, create_box, CellType

from precompute import (compute_scaled_jacobian_determinant,
                        compute_scaled_geometrical_factor,
                        compute_boundary_facets_scaled_jacobian_determinant)
from operators import (mass_operator, stiffness_operator)
from utils import facet_integration_domain

float_type = np.float64

# Source parameters
source_frequency = 0.5e6  # Hz
source_amplitude = 60000.0  # Pa
period = 1.0 / source_frequency  # s
angular_frequency = 2.0 * np.pi * source_frequency  # rad/s

# Material parameters
speed_of_sound = 1500.0  # m/s
density = 1000.0  # kg/m^3

# Domain parameters
domain_length = 0.12  # m

# FE parameters
basis_degree = 4
quadrature_degree = {
    2: 3,
    3: 4,
    4: 6,
    5: 8,
    6: 10,
    7: 12,
    8: 14,
    9: 16,
    10: 18,
}


# Read mesh and mesh tags
with XDMFFile(MPI.COMM_WORLD, "mesh.xdmf", "r") as fmesh:
    mesh_name = "planar_3d_0"
    mesh = fmesh.read_mesh(name=f"{mesh_name}")
    tdim = mesh.topology.dim
    gdim = mesh.geometry.dim
    mt_cell = fmesh.read_meshtags(mesh, name=f"{mesh_name}_cells")
    mesh.topology.create_connectivity(tdim-1, tdim)
    mt_facet = fmesh.read_meshtags(mesh, name=f"{mesh_name}_facets")

# N = 16
# mesh = create_box(
#     MPI.COMM_WORLD, ((0., 0., 0.), (1., 1., 1.)),
#     (N, N, N), cell_type=CellType.hexahedron, dtype=float_type)
# tdim = mesh.topology.dim
# gdim = mesh.geometry.dim

# Mesh parameters
num_cells = mesh.topology.index_map(tdim).size_local
hmin = np.array([cpp.mesh.h(
    mesh._cpp_object, tdim, np.arange(num_cells, dtype=np.int32)).min()])
mesh_size = np.zeros(1)
MPI.COMM_WORLD.Reduce(hmin, mesh_size, op=MPI.MIN, root=0)
MPI.COMM_WORLD.Bcast(mesh_size, root=0)

# Define a DG function space for the material parameters
V_DG = functionspace(mesh, ("DG", 0))
c0 = Function(V_DG)
c0.x.array[:] = speed_of_sound
c0_ = c0.x.array

rho0 = Function(V_DG)
rho0.x.array[:] = density
rho0_ = rho0.x.array

# Temporal parameters
CFL = 0.65
time_step_size = CFL * mesh_size / (speed_of_sound * basis_degree**2)
step_per_period = int(period / time_step_size) + 1
time_step_size = period / step_per_period
start_time = 0.0
final_time = domain_length / speed_of_sound + 8.0 / source_frequency
number_of_step = (final_time - start_time) / time_step_size + 1

# Mesh geometry data
x_dofs = mesh.geometry.dofmap
x_g = mesh.geometry.x
cell_type = mesh.basix_cell()

# Tensor product element
family = basix.ElementFamily.P
variant = basix.LagrangeVariant.gll_warped

basix_element = basix.create_tp_element(
    family, cell_type, basis_degree, variant)
element = basix.ufl._BasixElement(basix_element)  # basix ufl element

# Define function space and functions
V = functionspace(mesh, element)
dofmap = V.dofmap.list

# Define functions
u0 = Function(V, dtype=float_type)
g = Function(V, dtype=float_type)
un = Function(V, dtype=float_type)
vn = Function(V, dtype=float_type)

# Get the numpy array
u0_ = u0.x.array
g_ = g.x.array
un_ = un.x.array
vn_ = vn.x.array

# Compute geometric data of cell entities
pts, wts = basix.quadrature.make_quadrature(
    basix.CellType.hexahedron, quadrature_degree[basis_degree],
    basix.QuadratureType.gll
)
nq = wts.size

gelement = basix.create_element(family, cell_type, 1, dtype=float_type)
gtable = gelement.tabulate(1, pts)
dphi = gtable[1:, :, :, 0]

# Compute scaled Jacobian determinant (cell)
detJ = np.zeros((num_cells, nq), dtype=float_type)
compute_scaled_jacobian_determinant(detJ, (x_dofs, x_g), num_cells, dphi, wts)

# # Compute scaled geometrical factor (J^{-T}J_{-1}) 
# G = np.zeros((num_cells, nq, (3*(gdim-1))), dtype=float_type)
# compute_scaled_geometrical_factor(G, (x_dofs, x_g), num_cells, dphi, wts)

# Compute geometric data of boundary facet entities
boundary_facets1 = mt_facet.indices[mt_facet.values == 1]
boundary_facets2 = mt_facet.indices[mt_facet.values == 2]

boundary_data1 = facet_integration_domain(
    boundary_facets1, mesh)  # cells with boundary facets (source)
boundary_data2 = facet_integration_domain(
    boundary_facets2, mesh)  # cells with boundary facets (absorbing)
local_facet_dof = np.array(
    basix_element.entity_closure_dofs[2],
    dtype=np.int32)  # local DOF on facets

pts_f, wts_f = basix.quadrature.make_quadrature(
    basix.CellType.quadrilateral, quadrature_degree[basis_degree], 
    basix.QuadratureType.gll)
nq_f = wts_f.size

# Evaluation points on the facets of the reference hexahedron
pts_0 = pts_f[:, 0]
pts_1 = pts_f[:, 1]

pts_f = np.zeros((6, nq_f, 3), dtype=float_type)
pts_f[0, :, :] = np.c_[pts_0, pts_1, np.zeros(nq_f, dtype=float_type)]  # z = 0
pts_f[1, :, :] = np.c_[pts_0, np.zeros(nq_f, dtype=float_type), pts_1]  # y = 0
pts_f[2, :, :] = np.c_[np.zeros(nq_f, dtype=float_type), pts_0, pts_1]  # x = 0
pts_f[3, :, :] = np.c_[np.ones(nq_f, dtype=float_type), pts_0, pts_1]  # x = 1
pts_f[4, :, :] = np.c_[pts_0, np.ones(nq_f, dtype=float_type), pts_1]  # y = 1
pts_f[5, :, :] = np.c_[pts_0, pts_1, np.ones(nq_f, dtype=float_type)]  # z = 1

# Derivatives on the facets of the reference hexahedron
dphi_f = np.zeros((6, 3, nq_f, 8), dtype=float_type)

for f in range(6):
    gtable_f = gelement.tabulate(1, pts_f[f, :, :]).astype(float_type)
    dphi_f[f, :, :, :] = gtable_f[1:, :, :, 0]

# Compute scaled Jacobian determinant (source facets)
detJ_f1 = np.zeros((boundary_data1.shape[0], nq_f), dtype=float_type)
compute_boundary_facets_scaled_jacobian_determinant(
    detJ_f1, (x_dofs, x_g), boundary_data1, dphi_f, wts_f, float_type)

# Compute scaled Jacobian determinant (absorbing facets)
detJ_f2 = np.zeros((boundary_data2.shape[0], nq_f), dtype=float_type)
compute_boundary_facets_scaled_jacobian_determinant(
    detJ_f2, (x_dofs, x_g), boundary_data2, dphi_f, wts_f, float_type)

# Create boundary facets dofmap (source)
bfacet_dofmap1 = np.zeros(
    (boundary_data1.shape[0], local_facet_dof.shape[1]), dtype=np.int32)

for i, (cell, local_facet) in enumerate(boundary_data1):
    bfacet_dofmap1[i, :] = dofmap[cell][local_facet_dof[local_facet]]

# Create boundary facets dofmap (absorbing)
bfacet_dofmap2 = np.zeros(
    (boundary_data2.shape[0], local_facet_dof.shape[1]), dtype=np.int32)

for i, (cell, local_facet) in enumerate(boundary_data2):
    bfacet_dofmap2[i, :] = dofmap[cell][local_facet_dof[local_facet]]

# ------------ #
# Assemble LHS #
# ------------ #

coeff0 = 1.0 / rho0_ / c0_ / c0_  # material coefficient

m = u0_.copy()

# mass_operator(m, )