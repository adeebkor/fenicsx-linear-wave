"""
ADD DOC!
"""

import numpy as np
from mpi4py import MPI

import numba.cuda as cuda

import basix
import basix.ufl
from dolfinx.fem import functionspace, Function
from dolfinx.la import InsertMode
from dolfinx.mesh import create_box, CellType, GhostMode

from precompute import compute_scaled_jacobian_determinant
from operators import mass_operator
from scatterer import scatter_reverse

# MPI
comm = MPI.COMM_WORLD
rank = comm.rank

# Check if CUDA is available
if cuda.is_available():
    print("CUDA is available")

cuda.detect()
# cuda.select_device(rank)

print(f"{rank} : {cuda.get_current_device()}")

# Set float type
float_type = np.float64

P = 4  # Basis function order
Q = {
    2: 3,
    3: 4,
    4: 6,
    5: 8,
    6: 10,
    7: 12,
    8: 14,
    9: 16,
    10: 18,
}  # Quadrature degree

N = 4
mesh = create_box(
    comm, ((0., 0., 0.), (1., 1., 1.)),
    (N, N, N), cell_type=CellType.hexahedron, 
    ghost_mode=GhostMode.none,
    dtype=float_type
)

# Mesh geometry data
x_dofs = mesh.geometry.dofmap
x_g = mesh.geometry.x
cell_type = mesh.basix_cell()

# Tensor product element
family = basix.ElementFamily.P
variant = basix.LagrangeVariant.gll_warped
cell_type = mesh.basix_cell()

basix_element = basix.create_tp_element(family, cell_type, P, variant)
element = basix.ufl._BasixElement(basix_element)  # basix ufl element

# Create functions space
V = functionspace(mesh, element)
dofmap = V.dofmap.list
imap = V.dofmap.index_map

if rank == 0:
    print(f"Number of degrees-of-freedom: {imap.size_global}")

# Compute ghosts data in this process that are owned by other processes
nlocal = imap.size_local
nghost = imap.num_ghosts
owners = imap.owners
unique_owners, owners_size = np.unique(owners, return_counts=True)
owners_idx = np.argsort(owners)
owners_idx_d = cuda.to_device(owners_idx)

owners_offsets = np.cumsum(owners_size)
owners_offsets = np.insert(owners_offsets, 0, 0)

# Compute owned data by this process that are ghosts data in other process 
shared_dofs = imap.index_to_dest_ranks()
shared_ranks = np.unique(shared_dofs.array)

ghosts = []
for shared_rank in shared_ranks:
    for dof in range(nlocal):
        if shared_rank in shared_dofs.links(dof):
            ghosts.append(shared_rank)

ghosts = np.array(ghosts)
unique_ghosts, ghosts_size = np.unique(ghosts, return_counts=True)
ghosts_offsets = np.cumsum(ghosts_size)
ghosts_offsets = np.insert(ghosts_offsets, 0, 0)

all_requests = []

@cuda.jit
def copy_range(in_, out_, begin, end):
    thread_id = cuda.threadIdx.x
    block_id = cuda.blockIdx.x
    idx = thread_id + block_id * cuda.blockDim.x

    if idx < end - begin:
        out_[idx] = in_[idx + begin]
    

# Send
# send_buff_idx = np.zeros(np.sum(owners_size), dtype=np.int64)
# send_buff_idx[:] = imap.ghosts[owners_idx]
send_buff_idx = [np.zeros(size, dtype=np.int64) for size in owners_size]
for i, owner in enumerate(unique_owners):
    begin = owners_offsets[i]
    end = owners_offsets[i + 1]
    send_buff_idx[i] = imap.ghosts[owners_idx[begin:end]]

send_buff_idx_d = [cuda.to_device(send_buff) for send_buff in send_buff_idx]
for i, owner in enumerate(unique_owners):
    # reqs = comm.Isend(send_buff_idx_d[i], dest=owner)
    print(type(send_buff_idx_d[i]))
    # all_requests.append(reqs)

# Receive
recv_buff_idx = np.zeros(np.sum(ghosts_size), dtype=np.int64)
recv_buff_idx_d = cuda.to_device(recv_buff_idx)
for i, ghost in enumerate(unique_ghosts):
    begin = ghosts_offsets[i]
    end = ghosts_offsets[i + 1]
    reqr = comm.Irecv(recv_buff_idx_d[begin:end], source=ghost)
    all_requests.append(reqr)

MPI.Request.Waitall(all_requests)

recv_buff_idx = recv_buff_idx_d.copy_to_host()

ghosts_idx = recv_buff_idx - imap.local_range[0]
ghosts_idx_d = cuda.to_device(ghosts_idx)

owners_data_d = [owners_idx_d, owners_size, owners_offsets, unique_owners]
ghosts_data_d = [ghosts_idx_d, ghosts_size, ghosts_offsets, unique_ghosts]

print(f"{rank}: {owners_size}")
exit()

# Define function for testing
u0 = Function(V, dtype=float_type)
u0.interpolate(lambda x: 100 * np.sin(2*np.pi*x[0]) * np.cos(3*np.pi*x[1])
               * np.sin(4*np.pi*x[2]))
u_ = u0.x.array.copy()

# -------------------- #
# Test scatter reverse #
# -------------------- #

scatter_rev = scatter_reverse(
    comm, owners_data_d, ghosts_data_d, nlocal, float_type
)

# Allocate memory on the device
u_d = cuda.to_device(u_)
