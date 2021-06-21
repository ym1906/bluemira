# bluemira is an integrated inter-disciplinary design tool for future fusion
# reactors. It incorporates several modules, some of which rely on other
# codes, to carry out a range of typical conceptual fusion reactor design
# activities.
#
# Copyright (C) 2021 M. Coleman, J. Cook, F. Franza, I. Maione, S. McIntosh, J. Morris,
#                    D. Short
#
# bluemira is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# bluemira is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with bluemira; if not, see <https://www.gnu.org/licenses/>.

"""
Coil positioning routines (automatic and adjustable)
"""
import numpy as np
import re
from scipy.interpolate import interp1d
from scipy.optimize import minimize_scalar
from scipy.spatial import ConvexHull
from bluemira.base.constants import EPS
from BLUEPRINT.base.error import EquilibriaError, GeometryError
from bluemira.base.look_and_feel import bluemira_warn
from BLUEPRINT.geometry.boolean import (
    boolean_2d_common,
    boolean_2d_difference,
    boolean_2d_union,
)
from BLUEPRINT.geometry.geombase import Plane
from BLUEPRINT.geometry.geomtools import lengthnorm, loop_plane_intersect
from BLUEPRINT.geometry.loop import Loop
from BLUEPRINT.geometry.inscribedrect import inscribed_rect_in_poly
from BLUEPRINT.equilibria.coils import Coil, CoilSet, PF_COIL_NAME, Solenoid
from BLUEPRINT.equilibria.plotting import XZLPlotter, RegionPlotter
from BLUEPRINT.utilities import tools


class CoilPositioner:
    """
    Initial coil positioning tools for ab initio equilibrium design

    Parameters
    ----------
    R_0: float
        Machine major radius [m]
    A: float
        Plasma aspect ratio
    delta: float
        Plasma triangularity
    kappa: float
        Plasma elongation
    track: Loop object (x, z)
        Track along which PF coils are positioned
    x_cs: float
        Central Solenoid radius
    tk_cs: float
        Central Solenoid thickness either side
    n_PF: int
        Number of PF coils
    n_CS: int
        Number of CS modules
    csgap: float (default = 0.1)
        The gap between CS modules [m]
    rtype: str
        The type of reactor ['ST', 'Normal']. Used for default coil positioning
    cslayout: str
        The layout of the CS modules ['ITER', 'DEMO']
    """

    def __init__(
        self,
        R_0,
        A,
        delta,
        kappa,
        track,
        x_cs,
        tk_cs,
        n_PF,
        n_CS,
        csgap=0.1,
        rtype="Normal",
        cslayout="DEMO",
    ):
        self.ref = [R_0, 0]
        self.A = A
        self.R_0 = R_0
        self.delta = delta
        self.kappa = kappa
        self.track = track
        self.x_cs = x_cs
        self.tk_cs = tk_cs
        self.n_PF = n_PF
        self.n_CS = n_CS
        self.csgap = csgap
        self.rtype = rtype
        self.cslayout = cslayout

    def equispace_PF(self, track, n_PF):
        """
        Equally spaces PF coils around a TF coil boundary track, picking
        some starting positions for the uppermost and lowermost PF coil
        based on plasma shape considerations (mirror about X-points)
        """
        a = np.rad2deg(np.arctan(abs(self.delta) / self.kappa))
        if self.rtype == "Normal":
            au = 90 + a * 1.6
            al = -90 - a * 1.6
        elif self.rtype == "ST":
            au = 90 + a * 1.2
            al = -90 - a * 1.2
        try:
            argl = track.receive_projection(self.ref, al, get_arg=True)
        except ValueError:
            argl = 0
        try:
            argu = track.receive_projection(self.ref, au, get_arg=True)
        except ValueError:
            argu = len(track) - 1
        tf_loop = Loop(*track[argl : argu + 1])
        l_norm = lengthnorm(tf_loop["x"], tf_loop["z"])
        xint, zint = interp1d(l_norm, tf_loop["x"]), interp1d(l_norm, tf_loop["z"])
        pos = np.linspace(0, 1, n_PF)
        return [Coil(x, z) for x, z in zip(xint(pos), zint(pos))]

    def equispace_CS(self, x_cs, tk_cs, z_min, z_max, n_CS):
        """
        Defines a Solenoid object with equally spaced nCS modules
        """
        return Solenoid(x_cs, tk_cs, z_min, z_max, n_CS, gap=self.csgap)

    def demospace_CS(self, x_cs, tk_cs, z_min, z_max, n_CS):
        """
        Defines a Solenoid object with DEMO like layout of nCS modules
        """
        if n_CS <= 2 or n_CS % 2 == 0:
            bluemira_warn(
                "So was kann ich mit einem DEMO-spacing nicht machen. "
                "Stattdessen gib ich dir einen ITER-spacing CS."
            )
            return self.equispace_CS(x_cs, tk_cs, z_min, z_max, n_CS)
        length = ((z_max - z_min) - (n_CS - 1) * self.csgap) / (
            n_CS + 1
        )  # Module length
        a = np.linspace(1, n_CS * 2 - 1, n_CS)
        a[n_CS // 2 :] += 2
        a[n_CS // 2] = n_CS + 1  # Central module
        b = np.linspace(0, n_CS - 1, n_CS)
        z_cs = z_max * np.ones(n_CS)
        z_cs -= a * length / 2 + b * self.csgap
        heights = length / 2 * np.ones(n_CS)
        heights[n_CS // 2] = length  # Central module
        c = [Coil(x_cs, z, dx=tk_cs, dz=dz, ctype="CS") for z, dz in zip(z_cs, heights)]
        return Solenoid(x_cs, tk_cs, z_min, z_max, n_CS, gap=self.csgap, coils=c)

    def make_coilset(self, d_coil=0.5):
        """
        Returns a CoilSet object
        """
        coils = self.equispace_PF(self.track, self.n_PF)
        z_max = max(self.track.z)
        z_min = -z_max
        if self.n_CS != 0:
            if self.cslayout == "ITER":
                coils.append(
                    self.equispace_CS(self.x_cs, self.tk_cs, z_min, z_max, self.n_CS)
                )
            elif self.cslayout == "DEMO":
                coils.append(
                    self.demospace_CS(self.x_cs, self.tk_cs, z_min, z_max, self.n_CS)
                )
            else:
                raise ValueError("Elige entre ITER y DEMO. " "Mas opciones no hay.")
        return CoilSet(coils, self.R_0, d_coil=d_coil)


class XZLMapper:
    """
    Coil positioning tools for use in optimisation

    Parameters
    ----------
    pftrack: Loop object (x, z)
        Track along which PF coils are positioned
    cs_x: float
        Radius of the centre of the central solenoid [m]
    cs_zmin: float
        Minimum z location of the CS [m]
    cs_zmax: float
        Maximum z location of the CS [m]
    cs_gap: float
        Gap between modules of the CS [m]
    CS: bool
        Whether or not to XL map CS
    """

    def __init__(self, pftrack, cs_x=1, cs_zmin=1, cs_zmax=1, cs_gap=0.1, CS=False):

        self.pfloop = pftrack.copy()  # Stored as loop too
        self.pftrack = self.pfloop.interpolator()

        self.flag_CS = CS
        if self.flag_CS:
            self.Xcs = cs_x
            self.z_min = cs_zmin
            self.z_max = cs_zmax
            self.gap = cs_gap
            self.make_cstrack()
        else:  # Due diligence
            self.Xcs = None
            self.z_min = None
            self.z_max = None
            self.gap = None
            self.cstrack = None

        self.exclusions = None
        self.excl_zones = []
        self.excl_loops = None
        self.incl_loops = None
        self._coilset = None  # PLotting utility

    def make_cstrack(self):
        """
        Make a normalised straight segment track for the central solenoid.
        """
        z = [self.z_max, self.z_min]
        self.cstrack = {"L": interp1d(z, [0, 1]), "z": interp1d([0, 1], z)}

    @staticmethod
    def PFnorm(l_values, loop, point):
        """
        Função de otimização para o posicionamento das bobinas ao longo da
        pista
        """
        return (loop["x"](l_values) - point[0]) ** 2 + (
            loop["z"](l_values) - point[1]
        ) ** 2

    def xz_to_L(self, x, z):  # noqa (N802)
        """
        Translação de coordenadas (x, z) até coordenadas lineares normalizadas
        (L) para as bobinas PF
        """
        return minimize_scalar(
            self.PFnorm, method="bounded", args=(self.pftrack, [x, z]), bounds=[0, 1]
        ).x

    def L_to_xz(self, l_values):  # noqa (N802)
        """
        Translação de coordenadas lineares normalizadas (L) até coordenadas
        (x, z) para as bobinas PF
        """
        return self.pftrack["x"](l_values), self.pftrack["z"](l_values)

    def z_to_L(self, zc_vec):  # noqa (N802)
        """
        Convert z values for the CS in L values of the CS track.
        """
        zc_vec = np.sort(zc_vec)[::-1]
        if len(zc_vec) == 1:
            return np.array([0.5])
        z_edge = np.zeros(len(zc_vec))
        z_edge[0] = self.z_max - 2 * abs(self.z_max - zc_vec[0])
        for i in range(1, len(zc_vec) - 1):
            z_edge[i] = zc_vec[i] - (z_edge[i - 1] - zc_vec[i] - self.gap)
        z_edge[len(zc_vec) - 1] = self.z_min
        return self.cstrack["L"](z_edge)

    def L_to_zdz(self, l_values):
        """
        Convert L values for the CS track into z and dz values for the CS.
        """
        l_values = tools.clip(l_values, 0, 1)
        l_values = np.sort(l_values)
        z_edge = self.cstrack["z"](l_values)
        dz, zc = np.zeros(len(l_values)), np.zeros(len(l_values))
        dz[0] = abs(self.z_max - z_edge[0]) / 2
        zc[0] = self.z_max - dz[0]
        for i in range(1, len(l_values)):
            dz[i] = abs(z_edge[i - 1] - z_edge[i] - self.gap) / 2
            zc[i] = z_edge[i - 1] - dz[i] - self.gap
        # dz[-1] = abs(z_edge[-1]-self.Zmin-self.gap)/2
        # zc[-1] = self.Zmin+dz[-1]
        return self.Xcs * np.ones(len(l_values)), zc[::-1], dz[::-1]  # Coil numbering

    def get_Lmap(self, coilset, mapping):  # noqa (N802)
        """
        Calculates initial L vector and lb and ub constraints on L vector.

        Parameters
        ----------
        coilset: CoilSet object
            The coilset to map
        mapping: list or set
            List of PF coil names on the track

        Returns
        -------
        L: np.array(N)
            The initial position vector for the coilset position optimiser
        lb, ub: np.array(N), np.array(N)
            The lower and upper bounds on the L vector to be respected by the
            optimiser
        """
        self._coilset = coilset  # for plotting
        track_coils = len(mapping)
        l_values = np.zeros(track_coils)
        lb = np.zeros(track_coils)
        ub = np.zeros(track_coils)
        pf_coils = [coil for coil in coilset.coils.values() if coil.name in mapping]
        for i, coil in enumerate(pf_coils):
            loc = self.xz_to_L(coil.x, coil.z)
            if self.exclusions is not None:
                for ex in self.exclusions:
                    if ex[0] < loc < ex[1]:
                        back = -(loc - ex[0] + 2 * coil.rc / self.pfloop.length)
                        forw = ex[1] - loc + 2 * coil.rc / self.pfloop.length
                        if abs(back) >= abs(forw):
                            d_l = forw
                            break
                        else:
                            d_l = back
                            break
                    else:
                        d_l = 0
                l_values[i] = loc + d_l
                lb[i], ub[i] = self._get_bounds(l_values[i])
            else:
                l_values[i] = loc
                lb[i], ub[i] = 0, 1
        lb, ub = self._segment_tracks(lb, ub)
        # El vector L tiene que ser adjustado a sus nuevos limites
        l_values = tools.clip(l_values, lb, ub)
        if self.flag_CS:
            l_cs = np.zeros(coilset.n_CS)
            lbcs = np.zeros(coilset.n_CS)
            ubcs = np.ones(coilset.n_CS)
            z = []
            for i, coil in enumerate(coilset.coils.values()):
                if coil.ctype == "CS":
                    z.append(coil.z)
            z = np.sort(z)[::-1]
            l_cs = self.z_to_L(z)
            l_values = np.append(l_values, l_cs)
            lb = np.append(lb, lbcs)
            ub = np.append(ub, ubcs)
        return l_values, lb, ub

    def _get_bounds(self, l_values):
        """
        Generates an initial set of bounds for L based on the exclusion zones
        for the PF coils
        """
        e = [e for b in self.exclusions for e in b]
        lb, ub = 0, 1
        for ex in e:
            if l_values < ex:
                ub = ex
                break
            else:
                lb = ex
        return lb, ub

    @staticmethod
    def _segment_tracks(lb, ub):
        """
        Applies additional (silent) constraints, effectively chopping up a
        sub-track into two, so that two coils don't end up on top of each other
        """
        # beware of np.zeros_like!
        lb_new, ub_new = np.zeros(len(lb)), np.zeros(len(ub))
        lb, ub = list(lb), list(ub)
        flag = False
        last_n = -1
        for i, (lower, upper) in enumerate(zip(lb, ub)):
            n = lb.count(lower)
            if i == last_n:
                flag = False
            if n == 1:  # No duplicates
                flag = False
                lb_new[i] = lower
                ub_new[i] = upper
            elif n != 1 and flag is False:
                flag = True
                last_n = i + n
                delta = (upper - lower) / n
                for k, j in enumerate(range(i, i + n)):
                    lb_new[j] = upper - (k + 1) * delta
                    ub_new[j] = upper - k * delta
            else:
                continue
        return lb_new, ub_new

    def _get_unique_zone(self, zones):
        """
        Makes a single "cutting" shape. This is a cheap way of avoiding a
        complicated merging list, checking for overlaps between zones.

        Parameters
        ----------
        zones: List[Loop]
            The list of exclusion zones

        Returns
        -------
        joiner: Loop
            The boolean union of all the exclusion zones
        """
        self.excl_zones.extend(zones)

        joiner = self.pfloop.offset(-0.0001)
        joiner.close()
        for zone in self.excl_zones:
            joiner = boolean_2d_union(joiner, zone)[0]

        return joiner

    def add_exclusion_zones(self, zones):
        """
        Fügt der PFspulenbahn Aussschlusszonen hinzu

        Parameters
        ----------
        zones: list(Loop, Loop, ..)
            List of Loop exclusion zones in x, z coordinates
        """
        excl_zone = self._get_unique_zone(zones)

        self.incl_loops = boolean_2d_difference(self.pfloop, excl_zone)
        self.excl_loops = boolean_2d_common(self.pfloop, excl_zone)

        # Track start and end points
        p0 = self.pfloop.d2.T[0]
        p1 = self.pfloop.d2.T[-1]

        # Calculate exclusion sections in parametric space
        exclusions = []
        for i, excl in enumerate(self.excl_loops):
            # Check if the start point lies in the exclusion
            if np.allclose(p0, excl.d2.T[0]) or np.allclose(p0, excl.d2.T[-1]):
                start = 0
            else:
                start = self.xz_to_L(*excl.d2.T[0])

            # Check if the end point lies in the inclusion
            if np.allclose(p1, excl.d2.T[-1]) or np.allclose(p1, excl.d2.T[0]):
                stop = 1
            else:
                stop = self.xz_to_L(*excl.d2.T[-1])

            exclusions.append(sorted([start, stop]))

        # Sort by order in parametric space
        self.exclusions = sorted(exclusions, key=lambda x: x[0])

    def plot(self, ax=None):
        """
        Plot the XZLMapper.
        """
        return XZLPlotter(self, ax=ax)


class RegionMapper:
    """
    Coil positioning tools for use in optimisation for regions.

    Parameters
    ----------
    pfregions: dict(coil_name:Loop, coil_name:Loop, ...)
        Regions in which each PF coil resides. The loop objects must be 2d in x,z.

    """

    def __init__(self, pfregions):

        self.pfregions = pfregions

        self.regions = {}
        self.name_str = "R_{}"

        try:
            for pf_name, loop_reg in self.pfregions.items():
                self._region_setup(pf_name, loop_reg)
        except AttributeError:
            raise EquilibriaError("pfregions is not a dictionary")

        self.no_regions = len(self.regions)

        self.l_values = np.zeros((self.no_regions, 2))
        self.max_currents = np.zeros(self.no_regions)

    def _region_setup(self, pf_name, loop_reg):

        if all(loop_reg.y != 0):
            raise EquilibriaError("Loop object must be 2D in x,z for RegionMapper")

        region_name = self._name_converter(pf_name, True)
        self.regions[region_name] = RegionInterpolator(loop_reg)

    def _regionname(self, region):
        if not isinstance(region, str):
            return self.name_str.format(region)
        elif re.match("^R_[0-9]+([.][0-9]+)?$", region):
            return region
        elif re.match("^PF_[0-9]+([.][0-9]+)?$", region):
            return self._name_converter(region, True)
        else:
            raise NameError("RegionName not valid")

    def _name_converter(self, regionname, coil_to_region=False):
        num = int(regionname.split("_")[-1])
        if coil_to_region:
            return self.name_str.format(num)
        else:
            return PF_COIL_NAME.format(num)

    def add_region(self, pfregion):
        """
        Add an extra region to map.

        Parameters
        ----------
        pfregion: dict(coil_name:Loop)
            A region where a PF coil will reside

        """
        self.pfregions = {**self.pfregions, **pfregion}
        name, region = list(pfregion.items())[0]
        self.no_regions += 1
        self.l_values = np.zeros((self.no_regions, 2))
        self.max_currents = np.zeros(self.no_regions)
        self._region_setup(name, region)

    def L_to_xz(self, region, l_values):
        """
        Convert L values to x,z values for a given region.
        """
        reg = self.regions[self._regionname(region)]
        # l_values = self.region_coil_overlap(l_values)
        xv, zv = reg.to_xz(l_values)
        return xv, zv

    def xz_to_L(self, region, x, z):
        """
        Convert x,z values to L values for a given region.
        """
        reg = self.regions[self._regionname(region)]
        l_0, l_1 = reg.to_L(x, z)
        return l_0, l_1

    def get_Lmap(self, coilset):
        """
        Calculates initial L vector and sets lb and ub constraints on L vector.

        Parameters
        ----------
        coilset: CoilSet object
            A coilset object to map

        """
        self._coilset = coilset

        for no, region in enumerate(self.regions.keys()):
            try:
                coil = coilset[self._name_converter(region)]
            except KeyError:
                bluemira_warn(f"{self._name_converter(region)} not found in coilset")
                continue

            self.l_values[no] = self.xz_to_L(region, coil.x, coil.z)

        # Force all initial positions to be within region
        self.l_values = tools.clip(self.l_values, 0, 1).flatten()

        return (
            self.l_values,
            np.zeros_like(self.l_values),
            np.ones_like(self.l_values),
        )

    def get_size_current_limit(self):
        """
        Get maximum coil current while staying within region boundaries.

        Coils are set up as current per unit area therefore limiting the max current
        limits the area a coil covers.

        Returns
        -------
        max_currents: np.array
            Max current for coil location within region

        """
        for no, (name, region) in enumerate(self.regions.items()):
            coil = self._coilset.coils[self._name_converter(name)]
            self.max_currents[no] = coil._get_max_current(
                *inscribed_rect_in_poly(region.loop, (coil.x, coil.z))
            )

        return self.max_currents

    def plot(self, ax=None):
        """
        Plot the RegionMapper.
        """
        return RegionPlotter(self, ax=ax)


class RegionInterpolator:
    """
    Sets up a region for a PF coil to move within.

    We are treating the region as a flat surface.

    The normalisation occurs by cutting the shape in two axes and
    normalising over the cut length within the region.

    Currently this is limited to convex polygons (also know as convex hulls).
    Generalisation to all polygons is possible but unimplemented
    and possibly quite slow when converting from normalised to real coordinates.

    When the coil position provided is outside the given region the coil will
    be moved to the closest edge of the region.

    The mapping from outside to the edge of the region is not strictly defined.
    The only certainty is that the coil will be moved into the region.

    Parameters
    ----------
    loop: Loop
        Region to interpolate within

    """

    def __init__(self, loop):

        # Should all of this be in Loop? - see Loop.interpolator

        self.x = loop.x
        self.z = loop.z
        self.loop = loop

        self.check_loop_feasibility(loop)

        self.loop = loop
        self.z_min = min(self.loop.z)
        self.z_max = max(self.loop.z)

    def to_xz(self, l_values):
        """
        Convert L values to x,z values for xy_cut.

        Parameters
        ----------
        l_values: list(float, float)
            Coordinates in normalised space

        Returns
        -------
        x, z: float
            Coordinates in real space

        Raises
        ------
        GeometryError
            When loop is not a Convex Hull

        """
        l_0, l_1 = l_values
        z = self.z_min + (self.z_max - self.z_min) * l_1

        plane = Plane([0, 0, z], [1, 0, z], [0, 1, z])

        intersect = loop_plane_intersect(self.loop, plane)
        if len(intersect) == 1:
            x = intersect[0][0]
        elif len(intersect) == 2:
            x_min, x_max = sorted([intersect[0][0], intersect[1][0]])
            x = x_min + (x_max - x_min) * l_0
        else:
            raise GeometryError("Region must be a Convex Hull")

        return x, z

    def to_L(self, x, z):
        """
        Convert x.z values to L values for xy_cut.

        Parameters
        ----------
        x, z: float
            Coordinates in real space

        Returns
        -------
        l_0, l_1: float
            Coordinates in normalised space

        Raises
        ------
        GeometryError
            When loop is not a Convex Hull

        """
        l_1 = (z - self.z_min) / (self.z_max - self.z_min)
        l_1 = tools.clip(l_1, 0.0, 1.0)

        plane = Plane([x, 0, z], [x + 1, 0, z], [x, 1, z])
        intersect = loop_plane_intersect(self.loop, plane)

        return self._intersect_filter(x, l_1, intersect)

    def _intersect_filter(self, x, l_1, intersect):
        """
        Checks where points are based on number of intersections
        with a plane. Should initially be called with a plane involving z.

        No intersection could mean above 1 edge therefore a plane in xy
        is checked before recalling this function.
        If there is one intersection point we are on an edge (either bottom or top),
        if there is two intersection points we are in the region,
        otherwise the region is not a convex hull.

        Parameters
        ----------
        x: float
            x coordinate
        l_1: float
            Normalised z coordinate
        intersect: Plane
            A plane through xz

        Returns
        -------
        l_0, l_1: float
            Coordinates in normalised space

        Raises
        ------
        GeometryError
            When loop is not a Convex Hull
        """
        if intersect is None:
            plane = Plane([x, 0, 0], [x + 1, 0, 0], [x, 1, 0])
            intersect = loop_plane_intersect(self.loop, plane)
            l_0, l_1 = self._intersect_filter(
                x, l_1, [False] if intersect is None else intersect
            )
        elif len(intersect) == 2:
            x_min, x_max = sorted([intersect[0][0], intersect[1][0]])
            l_0 = tools.clip((x - x_min) / (x_max - x_min), 0.0, 1.0)
        elif len(intersect) == 1:
            l_0 = float(l_1 == 1.0)
        else:
            raise GeometryError("Region must be a Convex Hull")
        return l_0, l_1

    @staticmethod
    def check_loop_feasibility(loop):
        """
        Checks the provided region is a ConvexHull.

        This is a current limitation of RegionMapper
        not providing a 'smooth' interpolation surface.

        Parameters
        ----------
        loop: Loop object
            Region to check

        Raises
        ------
        GeometryError
            When loop is not a Convex Hull

        """
        if not np.allclose(ConvexHull(loop.d2.T).volume, loop.area, atol=EPS):
            raise GeometryError("Region must be a Convex Hull")


if __name__ == "__main__":
    from BLUEPRINT import test

    test()
