'''
    File: parametrization.py
    License: MIT
    Author: Aidan Kurtz
    Created: 25/08/2016
    Python Version: 3.5
    ========================
    Might be moving this somewhere else soon...
'''

import bisect
import math
import numpy as np
from scipy import sparse
import sys
import timeit

from visual import *
from machina import *
from utils import *
from transforms import *

# Compute the matchings for all pairs of face-adjacent tets.
# We use matchings to characterize closeness between two frames s and t.
# It is essentially the chiral permutation that most closely defines
# the rotation between them.
def compute_matchings(machina):
    # Loop through all pairs of face-adjacent tets.
    for fi, pair in enumerate(machina.tet_mesh.adjacent_elements):
        args = []
        if -1 in pair:
            continue # Boundary face
        # Find the best permutation to characterize closeness.
        for permutation in chiral_symmetries:
            arg = machina.frames[pair[0]].uvw - machina.frames[pair[1]].uvw * permutation.T
            args.append(np.linalg.norm(arg))
        # Store the matching, as an index into chiral symmetry group.
        machina.matchings[fi] = np.argmin(args)

def compute_edge_types(machina, edge_index):
    # Classify the internal edges by type, and find the singular graph.
    # The edge type is determined via concatenation of the matchings around 
    # the edge's tetrahedral one-ring.
    for ei in edge_index:
        try:
            one_ring = machina.one_rings[ei]
        except KeyError:
            continue # Not an internal edge.
        
        # Concatenate the matchings around the edge to find its type.
        edge_type = np.identity(3)
        for fi in one_ring['faces']:
            matching = []
            # Recall that in the one-ring, if 'fi' is negative, it is
            # a 't-s' pair, as opposed to a 's-t' pair.
            # If pair order is reversed, invert/transpose rotation matrix.
            # Use copysign to distinguish +0 from -0.
            if np.copysign(1, fi) > 0:
                matching = chiral_symmetries[machina.matchings[fi]]
            else:
                matching = chiral_symmetries[machina.matchings[-fi]].T
            # Concatenate transforms
            edge_type = np.dot(edge_type, matching)
        
        # Locate singular (not identity) and improper (not restricted) edges.
        is_singular, is_improper = True, True
        for si, restricted_type in enumerate(chiral_symmetries[0:9]):
            if np.allclose(edge_type, restricted_type):
                if si == 0 : is_singular = False
                is_improper = False
                break

        # Classify as proper(0), singular(1), improper (2)
        if is_singular: machina.edge_types[ei] = 1
        if is_improper: machina.edge_types[ei] = 2
        
# Computes singular graph, that is, all non-identity (singular)
# edges. Returns the singular edges, improper edges and singular
# vertices.
def singular_graph(machina):
    # Compute matchings.
    compute_matchings(machina)
    compute_edge_types(machina, range(len(machina.tet_mesh.edges)))
   
    # Store them in lists for output.
    singular_edges = []
    improper_edges = []
    singular_vertices = dict()
    # Store edges and vertices
    for ei, edge in enumerate(machina.tet_mesh.edges):     
        if machina.edge_types[ei] > 0:
            singular_edges.append(ei)
            # Store the vertices with associated singularity type.
            singular_vertices[edge[0]] = machina.edge_types[ei]
            singular_vertices[edge[1]] = machina.edge_types[ei]
        if machina.edge_types[ei] == 2:
            improper_edges.append(ei)
    # Output each graph as a .vtk.
    vtk_lines(machina.tet_mesh.points,
             [machina.tet_mesh.edges[ei] for ei in singular_edges],
             'singular')
    vtk_lines(machina.tet_mesh.points,
             [machina.tet_mesh.edges[ei] for ei in improper_edges],
             'improper')

    return singular_edges, improper_edges, singular_vertices

# Since the variable are flattened as vectors, where each tet has
# four vertices and each vertex has 3 coordinates. The arguments are
# tet index (ti), local vertex index (vi) and coordinate index (ci).
def var_index(ti, vi, ci):
    return ( 12 * ti + 3 * vi + ci )

def linear_system(machina, mst_edges, singular_vertices):
    
    ne = len(machina.tet_mesh.elements)
    C = sparse.lil_matrix( (3 * 12 * ne, 12*ne) )
    ccount = 0 # constraint counter

    for fi, adj_ti in enumerate(machina.tet_mesh.adjacent_elements):
        s, t = adj_ti[0], adj_ti[1]
        # Boundary face.
        if -1 in [s, t]:
            t = s if s != -1 else t # tet index
            vi_t = [] # local tet vertex indices of face.
            for vi in machina.tet_mesh.faces[fi]:
                vi_t.append(machina.tet_mesh.elements[t].index(vi))
            # Constrain surface normal.
            w_pqr = [ var_index(t, vi_t[i], 2) for i in range(3) ]
            for i in range(2):
                C[ccount, w_pqr[0]] = 1
                C[ccount, w_pqr[i]] = -1
                ccount += 1
            
        # Internal face with two tets in common.
        else:
            match = chiral_symmetries[machina.matchings[fi]]
            # Get local tet vertex indices of shared face vertices.
            vi_s, vi_t = [], []
            for vi in machina.tet_mesh.faces[fi]:
                vi_s.append(machina.tet_mesh.elements[s].index(vi))
                vi_t.append(machina.tet_mesh.elements[t].index(vi))

            # Next, apply constraints.
            # If gap is 0 (minimum spanning tree).
            if fi in mst_edges:
                for i in [0,1,2]: # points pqr
                    for j in [0,1,2]: # coords uvw
                        C[ccount, var_index(t, vi_t[i], j)] = - 1
                        for k in [0,1,2]:
                            C[ccount, var_index(s, vi_s[i], k)] = match[j, k]
                        ccount += 1
            # If gap isn't 0, enforce that it be constant.
            else:
                for i in [1,2]: # points qr
                    for j in [0,1,2]: # coords uvw
                        C[ccount, var_index(t, vi_t[0], j)] = 1
                        C[ccount, var_index(t, vi_t[i], j)] = - 1
                        for k in [0,1,2]: # permutation
                            C[ccount, var_index(s, vi_s[0], k)] = - match[j, k]
                            C[ccount, var_index(s, vi_s[i], k)] = match[j, k]
                        ccount += 1

    

    C = C.tocsr()
    num_nonzeros = np.diff(C.indptr)
    C = C[num_nonzeros != 0] # remove zero-rows

    # Create laplacian of tetrahedrons.
    L = sparse.diags([1,1,1,-3,1,1,1],[-9,-6,-3,0,3,6,9],(12*ne,12*ne))

    return L, C

def drop_rows(M, var_i):
    M = M.tolil()
    M.rows = np.delete(M.rows, var_i)
    M.data = np.delete(M.data, var_i)
    M._shape = (M._shape[0] - len(var_i), M._shape[1])
    return M

# Remove variables from system.
# The 'b' matrix should have its i value(s) set before calling.
def reduce_system(A, x, b, var_i):

    # Convert all the lil format.
    A = sparse.lil_matrix(A)
    x = sparse.lil_matrix(x.reshape((len(x),1)))
    b = sparse.lil_matrix(b.reshape((len(b),1)))

    # Update rhs b (absorbs vars).
    for i in var_i:
        b = b - x[i,0] * A.getcol(i)

    # Drop rows form b vector.
    b = drop_rows(b, var_i) 
    # Drop rows from the x vector.
    x = drop_rows(x, var_i)
    # Drop rows from the A matrix.
    A = drop_rows(A, var_i)
    # Drop cols from the A matrix.
    A = drop_rows(A.transpose(), var_i)

    return A, x, b

def flag_integer_vars(machina, singular_vertices):

    int_vars = set()

    # Iterate through all variables
    for ti, tet in enumerate(machina.tet_mesh.elements):
        for local_vi, vi in enumerate(tet):
            # Singular vertices constrain two of their variables.
            # If singularity type is Jw, then u, v must be integers.
            if vi in singular_vertices:
                if singular_vertices[vi] < 4:
                    int_vars.add(var_index(ti, local_vi, 0))
                    int_vars.add(var_index(ti, local_vi, 1))
                elif singular_vertices[vi] < 7:
                    int_vars.add(var_index(ti, local_vi, 1))
                    int_vars.add(var_index(ti, local_vi, 2))
                else: # Doesn't check for improper.
                    int_vars.add(var_index(ti, local_vi, 2))
                    int_vars.add(var_index(ti, local_vi, 0))
            # Surface vertices must be integer in w.
            if vi in machina.surf_mesh.vertex_map:
                int_vars.add(var_index(ti, local_vi, 2))
    
    return int_vars
            

def adaptive_rounding(machina, int_vars):
    pass

def parametrize_volume(machina, singular_vertices, h):

    # Each vertex has multiple values, depending
    # on the number of tets it's a part of.
    ne = len(machina.tet_mesh.elements)

    # Minimum spanning tree of dual mesh as list of face indices.
    # Span until all tets have been visited.
    ti = 0
    mst_edges = set()
    visited_tets = set()
    while ti < len(machina.tet_mesh.elements):
        for neigh_ti in machina.tet_mesh.neighbors[ti]:
            if neigh_ti in visited_tets or neigh_ti == -1:
                continue
            # Get face index from s-t tet pair.
            fi = machina.dual_edges[frozenset([ti, neigh_ti])]
            mst_edges.add(fi)
            visited_tets.add(ti)
        ti += 1

    print('Computing laplacian and constraints...')

    # Create linear system based on laplacian and constraints.
    laplacian, cons = linear_system(machina, mst_edges, singular_vertices)
    n_cons = cons.get_shape()[0]

    A = sparse.bmat(([[laplacian, cons.transpose()],[cons, None]]), dtype=np.int32)

    # Discrete frame divergence.
    b = np.zeros((12*ne + n_cons))
    for ti in range(ne):
        tet_vol = tet_volume(machina.tet_mesh, ti)
        frame = machina.frames[ti]
        div = [ np.sum(frame.uvw[0,0]),
                np.sum(frame.uvw[1,1]),
                np.sum(frame.uvw[2,2]) ]
        b[12*ti : 12*(ti+1)] = np.hstack([ div for _ in range(4)])

    b = np.divide(b, h)

    print("Conjugate Gradient... (Round 1)", end=" ")
    sys.stdout.flush()

    x, info = sparse.linalg.cg(A, b, tol = 1e-2)

    say_ok()




    print('Adaptive rounding...')
    int_vars = flag_integer_vars(machina, singular_vertices)
    # Enforce integer variables
    vars_fixed = dict()
    # The reduction array is used to keep track of global vs. reduced indices
    # as we progressively round the variables.
    # row index: reduced index, col 0: global index, col 1: is_int boolean.
    reduction_arr = np.zeros((12*ne, 2))
    reduction_arr[:,0] = np.arange(12*ne)
    for vi in int_vars:
        reduction_arr[vi,1] = 1

    # Loop until all integer variables are fixed.
    while (len(vars_fixed) < len(int_vars)):
        
        # Identify integer variables not yet fixed.
        vars_left = dict()
        for ri in range(reduction_arr.shape[0]):
            if reduction_arr[ri,1]:
                vars_left[ri] = reduction_arr[ri,0]

        print('Conjugate gradient... (%i integers left)' % len(vars_left))

        # Identify fixeable variables
        # First, variables with a small deviation should
        # be rounded to their nearest integer.
        vars_to_fix = []
        # gvi: global variable index, rvi: reduced variable index.
        for rvi, gvi in vars_left.items():
            value = x[rvi]
            rounded = int(round(value))
            if np.abs(value - rounded) > 1e-4:
                continue
            # Otherwise, delta is small enough to round.
            x[rvi] = rounded
            vars_fixed[gvi] = rounded
            vars_to_fix.append(rvi)

        # If no variable is fixed, fix the one with the smallest
        # deviation from its rounded integer.
        if len(vars_to_fix) == 0:
            rvi = np.argmin([ np.abs(x[rvi] - round(x[rvi])) for rvi in list(vars_left.keys()) ])
            x[rvi] = rounded(x[rvi])
            vars_fixed[vars_left[rvi]] = x[rvi]
            vars_to_fix.append(rvi)

        # Update linear system.
        A, x, b = reduce_system(A, x, b, vars_to_fix)
        b = b.toarray()
        x = x.toarray()

        # Run conjugate gradient on reduced system.
        x, info = sparse.linalg.cg(A, b, x0=x, tol = 1e-2)

        # Update the reduction array.
        reduction_arr = np.delete(reduction_arr, vars_to_fix, axis=0)


    uvw_map = np.zeros(12*ne)
    count = 0
    for i in range(12*ne):
        if i in vars_fixed:
            uvw_map[i] = vars_fixed[i]
            count += 1
        else:
            uvw_map[i] = x[i - count]

    return uvw_map
