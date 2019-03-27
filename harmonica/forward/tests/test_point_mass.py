"""
Test forward modellig for point masses.
"""
import numpy as np
import numpy.testing as npt
from pytest import raises

from ..point_mass import point_mass_gravity
from harmonica.constants import GRAVITATIONAL_CONST


def test_invalid_field():
    point_mass = [0.0, 0.0, 0.0]
    mass = 1.0
    longitude = np.array(0.0)
    latitude = np.array(0.0)
    height = np.array(0.0)
    with raises(ValueError):
        point_mass_gravity(
            [longitude, latitude, height], point_mass, mass, "this-field-does-not-exist"
        )


def test_point_mass_on_origin():
    "Check gravitational fields of point mass on origin"
    point_mass = [0.0, 0.0, 0.0]
    mass = 1.0
    radius = np.logspace(1, 8, 5, dtype="float64")
    longitude = np.linspace(-180, 180, 37)
    latitude = np.linspace(-90, 90, 19)
    longitude, latitude, radius = np.meshgrid(longitude, latitude, radius)
    potential = point_mass_gravity(
        [longitude, latitude, radius], point_mass, mass, "potential"
    )
    potential_analytical = GRAVITATIONAL_CONST * mass / radius
    npt.assert_allclose(potential, potential_analytical)
    gz = point_mass_gravity([longitude, latitude, radius], point_mass, mass, "gz")
    gz_analytical = -GRAVITATIONAL_CONST * mass / radius ** 2
    # Convert to mGal
    gz_analytical *= 1e5
    npt.assert_allclose(gz, gz_analytical)


def test_point_mass_constant_latitude_longitude():
    "Check gravity fields with same latitude and longitude coordinates"
    sphere_radius = 1.0
    mass = 1.0
    for longitude in np.linspace(-180, 180, 37):
        for latitude in np.linspace(-90, 90, 19):
            for height in np.logspace(0, 4, 5):
                point_mass = [longitude, latitude, sphere_radius]
                coordinates = [
                    np.array(longitude),
                    np.array(latitude),
                    np.array(height + sphere_radius),
                ]
                potential = point_mass_gravity(
                    coordinates, point_mass, mass, "potential"
                )
                potential_analytical = GRAVITATIONAL_CONST * mass / height
                npt.assert_allclose(potential, potential_analytical)
                gz = point_mass_gravity(coordinates, point_mass, mass, "gz")
                gz_analytical = -GRAVITATIONAL_CONST * mass / height ** 2
                # Convert to mGal
                gz_analytical *= 1e5
                npt.assert_allclose(gz, gz_analytical)