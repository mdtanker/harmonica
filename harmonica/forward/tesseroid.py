# Copyright (c) 2018 The Harmonica Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Forward modelling for tesseroids
"""
from numba import jit, prange
import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.optimize import minimize_scalar

from ..constants import GRAVITATIONAL_CONST
from .utils import distance_spherical
from .point_mass import (
    kernel_potential_spherical,
    kernel_g_z_spherical,
)

STACK_SIZE = 100
MAX_DISCRETIZATIONS = 100000
GLQ_DEGREES = (2, 2, 2)
DISTANCE_SIZE_RATII = {"potential": 1, "g_z": 2.5}
DELTA_RATIO = 0.1


def tesseroid_gravity(
    coordinates,
    tesseroids,
    density,
    field,
    parallel=True,
    radial_adaptive_discretization=False,
    dtype=np.float64,
    disable_checks=False,
):
    """
    Compute gravitational field of tesseroids on computation points.

    .. warning::

        The ``g_z`` field returns the downward component of the gravitational
        acceleration on the local North oriented coordinate system.
        It is equivalent to the opposite of the radial component, therefore
        it's positive if the acceleration vector points inside the spheroid.

    Parameters
    ----------
    coordinates : list or 1d-array
        List or array containing ``longitude``, ``latitude`` and ``radius`` of
        the computation points defined on a spherical geocentric coordinate
        system.
        Both ``longitude`` and ``latitude`` should be in degrees and ``radius``
        in meters.
    tesseroids : list or 1d-array
        List or array containing the coordinates of the tesseroid:
        ``w``, ``e``, ``s``, ``n``, ``bottom``, ``top`` under a geocentric
        spherical coordinate system.
        The longitudinal and latitudinal boundaries should be in degrees, while
        the radial ones must be in meters.
    density : list or array
        List or array containing the density of each tesseroid in kg/m^3.
    field : str
        Gravitational field that wants to be computed.
        The available fields are:

        - Gravitational potential: ``potential``
        - Downward acceleration: ``g_z``

    parallel : bool (optional)
        If True the computations will run in parallel using Numba built-in
        parallelization. If False, the forward model will run on a single core.
        Might be useful to disable parallelization if the forward model is run
        by an already parallelized workflow. Default to True.
    radial_adaptive_discretization : bool (optional)
        If ``False``, the adaptive discretization algorithm will split the
        tesseroid only on the horizontal direction.
        If ``True``, it will perform a three dimensional adaptive
        discretization, splitting the tesseroids on every direction.
        Default ``False``.
    dtype : data-type (optional)
        Data type assigned to the resulting gravitational field. Default to
        ``np.float64``.
    disable_checks : bool (optional)
        Flag that controls whether to perform a sanity check on the model.
        Should be set to ``True`` only when it is certain that the input model
        is valid and it does not need to be checked.
        Default to ``False``.

    Returns
    -------
    result : array
        Gravitational field generated by the tesseroids on the computation
        points.

    Examples
    --------

    >>> # Get WGS84 ellipsoid from the Boule package
    >>> import boule
    >>> ellipsoid = boule.WGS84
    >>> # Define tesseroid of 1km of thickness with top surface on the mean
    >>> # Earth radius
    >>> thickness = 1000
    >>> top = ellipsoid.mean_radius
    >>> bottom = top - thickness
    >>> w, e, s, n = -1.0, 1.0, -1.0, 1.0
    >>> tesseroid = [w, e, s, n, bottom, top]
    >>> # Set a density of 2670 kg/m^3
    >>> density = 2670.0
    >>> # Define computation point located on the top surface of the tesseroid
    >>> coordinates = [0, 0, ellipsoid.mean_radius]
    >>> # Compute radial component of the gravitational gradient in mGal
    >>> tesseroid_gravity(coordinates, tesseroid, density, field="g_z")
    array(112.54539933)

    """
    kernels = {"potential": kernel_potential_spherical, "g_z": kernel_g_z_spherical}
    if field not in kernels:
        raise ValueError("Gravitational field {} not recognized".format(field))
    # Figure out the shape and size of the output array
    cast = np.broadcast(*coordinates[:3])
    result = np.zeros(cast.size, dtype=dtype)
    # Convert coordinates and tesseroids to arrays
    coordinates = tuple(np.atleast_1d(i).ravel() for i in coordinates[:3])
    tesseroids = np.atleast_2d(tesseroids)
    # Sanity checks for tesseroids and computation points
    if not disable_checks:
        tesseroids = _check_tesseroids(tesseroids)
        _check_points_outside_tesseroids(coordinates, tesseroids)
    # Check if density are homogeneous or variable
    density_func = None
    if callable(density):
        density_func = jit(nopython=True)(density)
        density = None
        tesseroids = density_based_discretization(tesseroids, density_func)
    else:
        density = np.atleast_1d(density).ravel()
        if not disable_checks and density.size != tesseroids.shape[0]:
            raise ValueError(
                "Number of elements in density ({}) ".format(density.size)
                + "mismatch the number of tesseroids ({})".format(tesseroids.shape[0])
            )
    # Get GLQ unscaled nodes, weights and number of nodes for each small
    # tesseroid
    glq_nodes, glq_weights = glq_nodes_weights(GLQ_DEGREES)
    # Compute gravitational field
    dispatcher(parallel)(
        coordinates,
        tesseroids,
        density,
        density_func,
        result,
        DISTANCE_SIZE_RATII[field],
        radial_adaptive_discretization,
        glq_nodes,
        glq_weights,
        kernels[field],
        dtype,
    )
    result *= GRAVITATIONAL_CONST
    # Convert to more convenient units
    if field == "g_z":
        result *= 1e5  # SI to mGal
    return result.reshape(cast.shape)


# --------------------------------------
# Density-based discretization functions
# --------------------------------------


def density_based_discretization(tesseroids, density):
    """
    Apply density_based discretization to a collection of tesseroids
    """
    discretized_tesseroids = []
    for tesseroid in tesseroids:
        discretized_tesseroids.extend(_density_based_discretization(tesseroid, density))
    return np.atleast_2d(discretized_tesseroids)


def _density_based_discretization(tesseroid, density):
    """
    Applies density-based discretization to a single tesseroid

    Splits the tesseroid on the points of maximum density variance

    Parameters
    ----------
    tesseroid : tuple
    density : func

    Returns
    -------
    tesseroids : list
    """
    # Define normalized density
    def normalized_density(radius):
        return (density(radius) - density_min) / (density_max - density_min)

    # Get boundaries of original tesseroid
    w, e, s, n, bottom, top = tesseroid[:]
    # Get minimum and maximum values of the density
    density_min, density_max = density_minmax(density, bottom, top)
    # Return the original tesseroid if max and min densities are equal
    if np.isclose(density_min, density_max):
        return [tesseroid]
    # Store the size of the original tesseroid
    size_original_tesseroid = top - bottom
    # Initialize list of pending and output tesseroids
    pending, tesseroids = [tesseroid], []
    # Discretization of the tesseroid
    while pending:
        tesseroid = pending.pop(0)
        bottom, top = tesseroid[-2:]
        radius_split, max_diff = maximum_absolute_diff(normalized_density, bottom, top)
        size_ratio = (top - bottom) / size_original_tesseroid
        if max_diff * size_ratio > DELTA_RATIO:
            pending.append([w, e, s, n, radius_split, top])
            pending.append([w, e, s, n, bottom, radius_split])
        else:
            tesseroids.append([w, e, s, n, bottom, top])
    return tesseroids


def density_minmax(density, bottom, top):
    """
    Compute the minimum and maximum value of a bounded density
    """
    minimum = minimize_scalar(density, bounds=[bottom, top], method="bounded")
    maximum = minimize_scalar(
        lambda radius: -density(radius), bounds=[bottom, top], method="bounded"
    )
    return minimum.fun, -maximum.fun


def maximum_absolute_diff(normalized_density, bottom, top):
    """
    Compute maximum abs difference between normalized density and straight line

    The maximum difference is computed within the ``bottom`` and ``top``
    boundaries.
    """

    def absolute_difference(radius):
        """
        Define absolute difference between normalized density and straight line
        """
        return np.abs(
            normalized_density(radius)
            - straight_line(radius, normalized_density, bottom, top)
        )

    # Use scipy.optimize.minimize_scalar for maximizing the absolute difference
    result = minimize_scalar(
        lambda radius: -absolute_difference(
            radius
        ),  # put a minus to maximize the absolute_difference
        bounds=[bottom, top],
        method="bounded",
    )
    # Get maximum difference and the radius at which it takes place
    radius_split = result.x
    max_diff = -result.fun
    return radius_split, max_diff


def straight_line(radius, normalized_density, bottom, top):
    """
    Compute the reference straight line that joins points of normalized density
    """
    norm_density_bottom = normalized_density(bottom)
    norm_density_top = normalized_density(top)
    slope = (norm_density_top - norm_density_bottom) / (top - bottom)
    return slope * (radius - bottom) + norm_density_bottom


# -----------------------------------------------------------------


def dispatcher(parallel):
    """
    Return the parallelized or serialized forward modelling function
    """
    dispatchers = {
        True: jit_tesseroid_gravity_parallel,
        False: jit_tesseroid_gravity_serial,
    }
    return dispatchers[parallel]


def jit_tesseroid_gravity(
    coordinates,
    tesseroids,
    density,
    density_func,
    result,
    distance_size_ratio,
    radial_adaptive_discretization,
    glq_nodes,
    glq_weights,
    kernel,
    dtype,
):  # pylint: disable=too-many-locals,invalid-name,not-an-iterable
    """
    Compute gravitational field of tesseroids on computations points

    Perform adaptive discretization, convert each small tesseroid to equivalent
    point masses through GLQ and use point masses kernel functions to compute
    the gravitational field.

    Parameters
    ----------
    coordinates : tuple
        Tuple containing the coordinates of the computation points in spherical
        geocentric coordinate system in the following order:
        ``longitude``, ``latitude``, ``radius``.
        Each element of the tuple must be a 1d array.
        Both ``longitude`` and ``latitude`` should be in degrees and ``radius``
        in meters.
    tesseroids : 2d-array
        Array containing the boundaries of each tesseroid:
        ``w``, ``e``, ``s``, ``n``, ``bottom``, ``top`` under a geocentric
        spherical coordinate system.
        The array must have the following shape: (``n_tesseroids``, 6), where
        ``n_tesseroids`` is the total number of tesseroids.
        All tesseroids must have valid boundary coordinates.
        Horizontal boundaries should be in degrees while radial boundaries
        should be in meters.
    density : 1d-array
        Density of each tesseroid in SI units.
    density_func : function
        Variable density of every tesseroid in SI units.
    stack : 2d-array
        Empty array where tesseroids created by adaptive discretization
        algorithm will be processed.
    small_tesseroids : 2d-array
        Empty array where smaller tesseroids created by adaptive discretization
        algorithm will be stored.
    result : 1d-array
        Array where the gravitational effect of each tesseroid will be added.
    distance_size_ratio : float
        Value of the distance size ratio.
    radial_adaptive_discretization : bool
        If ``False``, the adaptive discretization algorithm will split the
        tesseroid only on the horizontal direction.
        If ``True``, it will perform a three dimensional adaptive
        discretization, splitting the tesseroids on every direction.
    glq_nodes : list
        List containing unscaled GLQ nodes.
    glq_weights : list
        List containing GLQ weights of the nodes.
    kernel : func
        Kernel function for the gravitational field of point masses.
    dtype : data-type
        Data type assigned to the resulting gravitational field.
    """
    # Get coordinates of the observation points
    # and precompute trigonometric functions
    longitude, latitude, radius = coordinates[:]
    longitude_rad = np.radians(longitude)
    cosphi = np.cos(np.radians(latitude))
    sinphi = np.sin(np.radians(latitude))
    # Loop over computation points
    for l in prange(longitude.size):
        # Initialize arrays to perform memory allocation only once
        stack = np.empty((STACK_SIZE, 6), dtype=dtype)
        small_tesseroids = np.empty((MAX_DISCRETIZATIONS, 6), dtype=dtype)
        # Loop over tesseroids
        for m in range(tesseroids.shape[0]):
            # Apply adaptive discretization on tesseroid
            n_splits = _adaptive_discretization(
                (longitude[l], latitude[l], radius[l]),
                tesseroids[m, :],
                distance_size_ratio,
                stack,
                small_tesseroids,
                radial_adaptive_discretization,
            )
            # Compute effect of the tesseroid through GLQ
            for tess_index in range(n_splits):
                tesseroid = small_tesseroids[tess_index, :]
                if density is None:
                    result[l] += gauss_legendre_quadrature(
                        longitude_rad[l],
                        cosphi[l],
                        sinphi[l],
                        radius[l],
                        tesseroid,
                        None,
                        density_func,
                        glq_nodes,
                        glq_weights,
                        kernel,
                    )
                else:
                    result[l] += gauss_legendre_quadrature(
                        longitude_rad[l],
                        cosphi[l],
                        sinphi[l],
                        radius[l],
                        tesseroid,
                        density[m],
                        None,
                        glq_nodes,
                        glq_weights,
                        kernel,
                    )


@jit(nopython=True)
def gauss_legendre_quadrature(
    longitude,
    cosphi,
    sinphi,
    radius,
    tesseroid,
    density,
    density_func,
    glq_nodes,
    glq_weights,
    kernel,
):  # pylint: disable=too-many-locals
    r"""
    Compute the effect of a tesseroid on a single observation point through GLQ

    The tesseroid is converted into a set of point masses located on the
    scaled nodes of the Gauss-Legendre Quadrature. The number of point masses
    created from each tesseroid is equal to the product of the GLQ degrees for
    each direction (:math:`N_r`, :math:`N_\lambda`, :math:`N_\phi`). The mass
    of each point mass is defined as the product of the tesseroid density
    (:math:`\rho`), the GLQ weights for each direction (:math:`W_i^r`,
    :math:`W_j^\phi`, :math:`W_k^\lambda`), the scale constant :math:`A` and
    the :math:`\kappa` factor evaluated on the coordinates of the point mass.

    Parameters
    ----------
    longitude : float
        Longitudinal coordinate of the observation points in radians.
    cosphi : float
        Cosine of the latitudinal coordinate of the observation point in
        radians.
    sinphi : float
        Sine of the latitudinal coordinate of the observation point in
        radians.
    radius : float
        Radial coordinate of the observation point in meters.
    tesseroids : 1d-array
        Array containing the boundaries of the tesseroid:
        ``w``, ``e``, ``s``, ``n``, ``bottom``, ``top``.
        Horizontal boundaries should be in degrees and radial boundaries in
        meters.
    density : float
        Density of the tesseroid in SI units.
    density_func : function
        Variable density of every tesseroid in SI units.
    glq_nodes : list
        Unscaled location of GLQ nodes for each direction.
    glq_weights : list
        GLQ weigths for each node for each direction.
    kernel : func
        Kernel function for the gravitational field of point masses.

    """
    # Get tesseroid boundaries
    w, e, s, n, bottom, top = tesseroid[:]
    # Calculate the A factor for the tesseroid
    a_factor = 1 / 8 * np.radians(e - w) * np.radians(n - s) * (top - bottom)
    # Unpack nodes and weights
    lon_nodes, lat_nodes, rad_nodes = glq_nodes[:]
    lon_weights, lat_weights, rad_weights = glq_weights[:]
    # Compute effect of the tesseroid on the observation point
    # by iterating over the location of the point masses
    # (move the iteration along the longitudinal nodes to the bottom for
    # optimization: reduce the number of times that the trigonometric functions
    # are evaluated)
    result = 0.0
    for j, lat_node in enumerate(lat_nodes):
        # Get the latitude of the point mass
        latitude_p = np.radians(0.5 * (n - s) * lat_node + 0.5 * (n + s))
        cosphi_p = np.cos(latitude_p)
        sinphi_p = np.sin(latitude_p)
        for k, rad_node in enumerate(rad_nodes):
            # Get the radius of the point mass
            radius_p = 0.5 * (top - bottom) * rad_node + 0.5 * (top + bottom)
            # Get kappa constant for the point mass
            kappa = radius_p ** 2 * cosphi_p
            for i, lon_node in enumerate(lon_nodes):
                # Get the longitude of the point mass
                longitude_p = np.radians(0.5 * (e - w) * lon_node + 0.5 * (e + w))
                # Compute the mass of the point mass
                mass = (
                    a_factor * kappa * lon_weights[i] * lat_weights[j] * rad_weights[k]
                )
                if density is None:
                    mass *= density_func(radius_p)
                else:
                    mass *= density
                # Add effect of the current point mass to the result
                result += mass * kernel(
                    longitude,
                    cosphi,
                    sinphi,
                    radius,
                    longitude_p,
                    cosphi_p,
                    sinphi_p,
                    radius_p,
                )
    return result


def glq_nodes_weights(glq_degrees):
    """
    Calculate GLQ unscaled nodes and weights

    Parameters
    ----------
    glq_degrees : list
        List of GLQ degrees for each direction: ``longitude``, ``latitude``,
        ``radius``.

    Returns
    -------
    glq_nodes : list
        Unscaled GLQ nodes for each direction: ``longitude``, ``latitude``,
        ``radius``.
    glq_weights : list
        GLQ weights for each node on each direction: ``longitude``,
        ``latitude``, ``radius``.
    """
    # Unpack GLQ degrees
    lon_degree, lat_degree, rad_degree = glq_degrees[:]
    # Get nodes coordinates and weights
    lon_node, lon_weights = leggauss(lon_degree)
    lat_node, lat_weights = leggauss(lat_degree)
    rad_node, rad_weights = leggauss(rad_degree)
    # Reorder nodes and weights
    glq_nodes = (lon_node, lat_node, rad_node)
    glq_weights = (lon_weights, lat_weights, rad_weights)
    return glq_nodes, glq_weights


@jit(nopython=True)
def _adaptive_discretization(
    coordinates,
    tesseroid,
    distance_size_ratio,
    stack,
    small_tesseroids,
    radial_discretization=False,
):
    """
    Perform the adaptive discretization algorithm on a tesseroid

    It apply the three or two dimensional adaptive discretization algorithm on
    a tesseroid after a single computation point.

    Parameters
    ----------
    coordinates : array
        Array containing ``longitude``, ``latitude`` and ``radius`` of a single
        computation point.
    tesseroid : array
        Array containing the boundaries of the tesseroid.
    distance_size_ratio : float
        Value for the distance-size ratio. A greater value will perform more
        discretizations.
    stack : 2d-array
        Array with shape ``(6, stack_size)`` that will temporarly hold the
        small tesseroids that are not yet processed.
        If too many discretizations will take place, increase the
        ``stack_size``.
    small_tesseroids : 2d-array
        Array with shape ``(6, MAX_DISCRETIZATIONS)`` that will contain every
        small tesseroid produced by the adaptive discretization algorithm.
        If too many discretizations will take place, increase the
        ``MAX_DISCRETIZATIONS``.
    radial_discretization : bool (optional)
        If ``True`` the three dimensional adaptive discretization will be
        applied.
        If ``False`` the two dimensional adaptive discretization will be
        applied, i.e. the tesseroid will only be split on the ``longitude`` and
        ``latitude`` directions.
        Default ``False``.

    Returns
    -------
    n_splits : int
        Total number of small tesseroids generated by the algorithm.
    """
    # Create stack of tesseroids
    stack[0] = tesseroid
    stack_top = 0
    n_splits = 0
    while stack_top >= 0:
        # Pop the first tesseroid from the stack
        tesseroid = stack[stack_top]
        stack_top -= 1
        # Get its dimensions
        l_lon, l_lat, l_rad = _tesseroid_dimensions(tesseroid)
        # Get distance between computation point and center of tesseroid
        distance = _distance_tesseroid_point(coordinates, tesseroid)
        # Check inequality
        n_lon, n_lat, n_rad = 1, 1, 1
        if distance / l_lon < distance_size_ratio:
            n_lon = 2
        if distance / l_lat < distance_size_ratio:
            n_lat = 2
        if distance / l_rad < distance_size_ratio and radial_discretization:
            n_rad = 2
        # Apply discretization
        if n_lon * n_lat * n_rad > 1:
            # Raise error if stack overflow
            # Number of tesseroids in stack = stack_top + 1
            if (stack_top + 1) + n_lon * n_lat * n_rad > stack.shape[0]:
                raise OverflowError("Stack Overflow. Try to increase the stack size.")
            stack_top = _split_tesseroid(
                tesseroid, n_lon, n_lat, n_rad, stack, stack_top
            )
        else:
            # Raise error if small_tesseroids overflow
            if n_splits + 1 > small_tesseroids.shape[0]:
                raise OverflowError(
                    "Exceeded maximum discretizations."
                    + " Please increase the MAX_DISCRETIZATIONS."
                )
            small_tesseroids[n_splits] = tesseroid
            n_splits += 1
    return n_splits


@jit(nopython=True)
def _split_tesseroid(
    tesseroid, n_lon, n_lat, n_rad, stack, stack_top
):  # pylint: disable=too-many-locals
    """
    Split tesseroid along each dimension
    """
    w, e, s, n, bottom, top = tesseroid[:]
    # Compute differential distance
    d_lon = (e - w) / n_lon
    d_lat = (n - s) / n_lat
    d_rad = (top - bottom) / n_rad
    for i in range(n_lon):
        for j in range(n_lat):
            for k in range(n_rad):
                stack_top += 1
                stack[stack_top, 0] = w + d_lon * i
                stack[stack_top, 1] = w + d_lon * (i + 1)
                stack[stack_top, 2] = s + d_lat * j
                stack[stack_top, 3] = s + d_lat * (j + 1)
                stack[stack_top, 4] = bottom + d_rad * k
                stack[stack_top, 5] = bottom + d_rad * (k + 1)
    return stack_top


@jit(nopython=True)
def _tesseroid_dimensions(tesseroid):
    """
    Calculate the dimensions of the tesseroid.
    """
    w, e, s, n, bottom, top = tesseroid[:]
    w, e, s, n = np.radians(w), np.radians(e), np.radians(s), np.radians(n)
    latitude_center = (n + s) / 2
    l_lat = top * np.arccos(np.sin(n) * np.sin(s) + np.cos(n) * np.cos(s))
    l_lon = top * np.arccos(
        np.sin(latitude_center) ** 2 + np.cos(latitude_center) ** 2 * np.cos(e - w)
    )
    l_rad = top - bottom
    return l_lon, l_lat, l_rad


@jit(nopython=True)
def _distance_tesseroid_point(
    coordinates, tesseroid
):  # pylint: disable=too-many-locals
    """
    Distance between a computation point and the center of a tesseroid
    """
    # Get center of the tesseroid
    w, e, s, n, bottom, top = tesseroid[:]
    longitude_p = (w + e) / 2
    latitude_p = (s + n) / 2
    radius_p = (bottom + top) / 2
    # Get distance between computation point and tesseroid center
    distance = distance_spherical(coordinates, (longitude_p, latitude_p, radius_p))
    return distance


def _check_tesseroids(tesseroids):  # pylint: disable=too-many-branches
    """
    Check if tesseroids boundaries are well defined

    A valid tesseroid should have:
        - latitudinal boundaries within the [-90, 90] degrees interval,
        - north boundaries greater or equal than the south boundaries,
        - radial boundaries positive or zero,
        - top boundaries greater or equal than the bottom boundaries,
        - longitudinal boundaries within the [-180, 360] degrees interval,
        - longitudinal interval must not be greater than one turn around the
          globe.

    Some valid tesseroids have its west boundary greater than the east one,
    e.g. ``(350, 10, ...)``. On these cases the ``_longitude_continuity``
    function is applied in order to move the longitudinal coordinates to the
    [-180, 180) interval. Any valid tesseroid should have east boundaries
    greater than the west boundaries before or after applying longitude
    continuity.

    Parameters
    ----------
    tesseroids : 2d-array
        Array containing the boundaries of the tesseroids in the following
        order: ``w``, ``e``, ``s``, ``n``, ``bottom``, ``top``.
        Longitudinal and latitudinal boundaries must be in degrees.
        The array must have the following shape: (``n_tesseroids``, 6), where
        ``n_tesseroids`` is the total number of tesseroids.

    Returns
    -------
    tesseroids :  2d-array
        Array containing the boundaries of the tesseroids.
        If no longitude continuity needs to be applied, the returned array is
        the same one as the orignal.
        Otherwise, it's copied and its longitudinal boundaries are modified.
    """
    west, east, south, north, bottom, top = tuple(tesseroids[:, i] for i in range(6))
    err_msg = "Invalid tesseroid or tesseroids. "
    # Check if latitudinal boundaries are inside the [-90, 90] interval
    invalid = np.logical_or(
        np.logical_or(south < -90, south > 90), np.logical_or(north < -90, north > 90)
    )
    if (invalid).any():
        err_msg += (
            "The latitudinal boundaries must be inside the [-90, 90] "
            + "degrees interval.\n"
        )
        for tess in tesseroids[invalid]:
            err_msg += "\tInvalid tesseroid: {}\n".format(tess)
        raise ValueError(err_msg)
    # Check if south boundary is not greater than the corresponding north
    # boundary
    invalid = south > north
    if (invalid).any():
        err_msg += "The south boundary can't be greater than the north one.\n"
        for tess in tesseroids[invalid]:
            err_msg += "\tInvalid tesseroid: {}\n".format(tess)
        raise ValueError(err_msg)
    # Check if radial boundaries are positive or zero
    invalid = np.logical_or(bottom < 0, top < 0)
    if (invalid).any():
        err_msg += "The bottom and top radii should be positive or zero.\n"
        for tess in tesseroids[invalid]:
            err_msg += "\tInvalid tesseroid: {}\n".format(tess)
        raise ValueError(err_msg)
    # Check if top boundary is not greater than the corresponding bottom
    # boundary
    invalid = bottom > top
    if (invalid).any():
        err_msg += "The bottom radius boundary can't be greater than the top one.\n"
        for tess in tesseroids[invalid]:
            err_msg += "\tInvalid tesseroid: {}\n".format(tess)
        raise ValueError(err_msg)
    # Check if longitudinal boundaries are inside the [-180, 360] interval
    invalid = np.logical_or(
        np.logical_or(west < -180, west > 360), np.logical_or(east < -180, east > 360)
    )
    if (invalid).any():
        err_msg += (
            "The longitudinal boundaries must be inside the [-180, 360] "
            + "degrees interval.\n"
        )
        for tess in tesseroids[invalid]:
            err_msg += "\tInvalid tesseroid: {}\n".format(tess)
        raise ValueError(err_msg)
    # Apply longitude continuity if w > e
    if (west > east).any():
        tesseroids = _longitude_continuity(tesseroids)
        west, east, south, north, bottom, top = tuple(
            tesseroids[:, i] for i in range(6)
        )
    # Check if west boundary is not greater than the corresponding east
    # boundary, even after applying the longitude continuity
    invalid = west > east
    if (invalid).any():
        err_msg += "The west boundary can't be greater than the east one.\n"
        for tess in tesseroids[invalid]:
            err_msg += "\tInvalid tesseroid: {}\n".format(tess)
        raise ValueError(err_msg)
    # Check if the longitudinal interval is not grater than one turn around the
    # globe
    invalid = east - west > 360
    if (invalid).any():
        err_msg += (
            "The difference between east and west boundaries cannot be greater than "
            + "one turn around the globe.\n"
        )
        for tess in tesseroids[invalid]:
            err_msg += "\tInvalid tesseroid: {}\n".format(tess)
        raise ValueError(err_msg)
    return tesseroids


def _check_points_outside_tesseroids(
    coordinates, tesseroids
):  # pylint: disable=too-many-locals
    """
    Check if computation points are not inside the tesseroids

    Parameters
    ----------
    coordinates : 2d-array
        Array containing the coordinates of the computation points in the
        following order: ``longitude``, ``latitude`` and ``radius``.
        Both ``longitude`` and ``latitude`` must be in degrees.
        The array must have the following shape: (3, ``n_points``), where
        ``n_points`` is the total number of computation points.
    tesseroids : 2d-array
        Array containing the boundaries of the tesseroids in the following
        order: ``w``, ``e``, ``s``, ``n``, ``bottom``, ``top``.
        Longitudinal and latitudinal boundaries must be in degrees.
        The array must have the following shape: (``n_tesseroids``, 6), where
        ``n_tesseroids`` is the total number of tesseroids.
        This array of tesseroids must have longitude continuity and valid
        boundaries.
        Run ``_check_tesseroids`` before.
    """
    longitude, latitude, radius = coordinates[:]
    west, east, south, north, bottom, top = tuple(tesseroids[:, i] for i in range(6))
    # Longitudinal boundaries of the tesseroid must be compared with
    # longitudinal coordinates of computation points when moved to
    # [0, 360) and [-180, 180).
    longitude_360 = longitude % 360
    longitude_180 = ((longitude + 180) % 360) - 180
    inside_longitude = np.logical_or(
        np.logical_and(
            west < longitude_360[:, np.newaxis], longitude_360[:, np.newaxis] < east
        ),
        np.logical_and(
            west < longitude_180[:, np.newaxis], longitude_180[:, np.newaxis] < east
        ),
    )
    inside_latitude = np.logical_and(
        south < latitude[:, np.newaxis], latitude[:, np.newaxis] < north
    )
    inside_radius = np.logical_and(
        bottom < radius[:, np.newaxis], radius[:, np.newaxis] < top
    )
    # Build array of booleans.
    # The (i, j) element is True if the computation point i is inside the
    # tesseroid j.
    inside = inside_longitude * inside_latitude * inside_radius
    if inside.any():
        err_msg = (
            "Found computation point inside tesseroid. "
            + "Computation points must be outside of tesseroids.\n"
        )
        for point_i, tess_i in np.argwhere(inside):
            err_msg += "\tComputation point '{}' found inside tesseroid '{}'\n".format(
                coordinates[:, point_i], tesseroids[tess_i, :]
            )
        raise ValueError(err_msg)


def _longitude_continuity(tesseroids):
    """
    Modify longitudinal boundaries of tesseroids to ensure longitude continuity

    Longitudinal boundaries of the tesseroids are moved to the ``[-180, 180)``
    degrees interval in case the ``west`` boundary is numerically greater than
    the ``east`` one.

    Parameters
    ----------
    tesseroids : 2d-array
        Longitudinal and latitudinal boundaries must be in degrees.
        Array containing the boundaries of each tesseroid:
        ``w``, ``e``, ``s``, ``n``, ``bottom``, ``top`` under a geocentric
        spherical coordinate system.
        The array must have the following shape: (``n_tesseroids``, 6), where
        ``n_tesseroids`` is the total number of tesseroids.

    Returns
    -------
    tesseroids : 2d-array
        Modified boundaries of the tesseroids.
    """
    # Copy the tesseroids to avoid modifying the original tesseroids array
    tesseroids = tesseroids.copy()
    west, east = tesseroids[:, 0], tesseroids[:, 1]
    tess_to_be_changed = west > east
    east[tess_to_be_changed] = ((east[tess_to_be_changed] + 180) % 360) - 180
    west[tess_to_be_changed] = ((west[tess_to_be_changed] + 180) % 360) - 180
    return tesseroids


# Define jitted versions of the forward modelling function
# pylint: disable=invalid-name
jit_tesseroid_gravity_serial = jit(nopython=True)(jit_tesseroid_gravity)
jit_tesseroid_gravity_parallel = jit(nopython=True, parallel=True)(
    jit_tesseroid_gravity
)
