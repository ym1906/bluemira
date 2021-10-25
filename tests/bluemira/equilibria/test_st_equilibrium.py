# bluemira is an integrated inter-disciplinary design tool for future fusion
# reactors. It incorporates several modules, some of which rely on other
# codes, to carry out a range of typical conceptual fusion reactor design
# activities.
#
# Copyright (C) 2021 M. Coleman, J. Cook, F. Franza, I.A. Maione, S. McIntosh, J. Morris,
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
BLUEPRINT -> bluemira ST equilibrium recursion test
"""

import os
import numpy as np
import pytest
from bluemira.base.file import get_bluemira_root
from bluemira.equilibria import (
    Equilibrium,
    CustomProfile,
    Grid,
    CoilSet,
    MagneticConstraintSet,
    IsofluxConstraint,
    Norm2Tikhonov,
    Coil,
    SymmetricCircuit,
    PicardDeltaIterator,
)
from bluemira.equilibria.physics import calc_li
from bluemira.equilibria.file import EQDSKInterface
from bluemira.equilibria.solve import DudsonConvergence


@pytest.mark.private
class TestSTEquilibrium:
    @classmethod
    def setup_class(cls):
        # Load reference and input data
        root = get_bluemira_root()
        private = os.path.split(root)[0]
        private = os.sep.join([private, "bluemira-private-data/equilibria/STEP_SPR_08"])
        eq_name = "STEP_SPR08_BLUEPRINT.json"
        filename = os.sep.join([private, eq_name])
        cls.eq_blueprint = Equilibrium.from_eqdsk(filename)
        jeq_name = "jetto.eqdsk_out"
        filename = os.sep.join([private, jeq_name])
        cls.profiles = CustomProfile.from_eqdsk(filename)
        reader = EQDSKInterface()
        cls.jeq_dict = reader.read(filename)

    def test_equilibrium(self):
        build_tweaks = {
            "plot_fbe_evol": True,
            "plot_fbe": True,
            "sol_isoflux": True,
            "process_midplane_iso": True,
            "tikhonov_gamma": 1e-8,
            "fbe_convergence": "Dudson",
            "fbe_convergence_crit": 1.0e-6,
            "nx_number_x": 7,
            "nz_number_z": 8,
        }

        R_0 = 3.639
        A = 1.667
        i_p = 20975205.2  # (EQDSK)

        xc = np.array(
            [1.5, 1.5, 8.259059936102478, 8.259059936102478, 10.635505223274231]
        )
        zc = np.array([8.78, 11.3, 11.8, 6.8, 1.7])
        dxc = np.array([0.175, 0.25, 0.25, 0.25, 0.35])
        dzc = np.array([0.5, 0.4, 0.4, 0.4, 0.5])

        coils = []
        for i, (x, z, dx, dz) in enumerate(zip(xc, zc, dxc, dzc)):
            coil = SymmetricCircuit(
                Coil(x=x, z=z, dx=dx, dz=dz, name=f"PF_{i+1}", ctype="PF")
            )
            coils.append(coil)
        coilset = CoilSet(coils)

        grid = Grid(
            x_min=0.0,
            x_max=max(xc + dxc) + 0.5,
            z_min=-max(zc + dzc),
            z_max=max(zc + dzc),
            nx=2 ** build_tweaks["nx_number_x"] + 1,
            nz=2 ** build_tweaks["nz_number_z"] + 1,
        )

        inboard_iso = [R_0 * (1.0 - 1 / A), 0.0]
        outboard_iso = [R_0 * (1.0 + 1 / A), 0.0]

        x = self.jeq_dict["xbdry"]
        z = self.jeq_dict["zbdry"]
        upper_iso = [x[np.argmax(z)], np.max(z)]
        lower_iso = [x[np.argmin(z)], np.min(z)]

        x_core = np.array([inboard_iso[0], upper_iso[0], outboard_iso[0], lower_iso[0]])
        z_core = np.array([inboard_iso[1], upper_iso[1], outboard_iso[1], lower_iso[1]])

        # Points chosen to replicate divertor legs in AH's FIESTA demo
        x_hfs = np.array(
            [
                1.42031,
                1.057303,
                0.814844,
                0.669531,
                0.621094,
                0.621094,
                0.645312,
                0.596875,
            ]
        )
        z_hfs = np.array(
            [4.79844, 5.0875, 5.37656, 5.72344, 6.0125, 6.6484, 6.82188, 7.34219]
        )
        x_lfs = np.array(
            [1.85625, 2.24375, 2.53438, 2.89766, 3.43047, 4.27813, 5.80391, 6.7]
        )
        z_lfs = np.array(
            [4.79844, 5.37656, 5.83906, 6.24375, 6.59063, 6.76406, 6.70625, 6.70625]
        )

        x_div = np.concatenate([x_lfs, x_lfs, x_hfs, x_hfs])
        z_div = np.concatenate([z_lfs, -z_lfs, z_hfs, -z_hfs])

        # Scale up Agnieszka isoflux constraints
        size_scaling = R_0 / 2.5
        x_div = size_scaling * x_div
        z_div = size_scaling * z_div

        xx = np.concatenate([x_core, x_div])
        zz = np.concatenate([z_core, z_div])

        constraint_set = MagneticConstraintSet(
            [IsofluxConstraint(xx, zz, ref_x=inboard_iso[0], ref_z=inboard_iso[1])]
        )

        initial_psi = self._make_initial_psi(
            coilset,
            grid,
            constraint_set,
            R_0 + 0.5,
            0,
            i_p,
            build_tweaks["tikhonov_gamma"],
        )

        eq = Equilibrium(coilset, grid, force_symmetry=True, psi=initial_psi, Ip=i_p)
        optimiser = Norm2Tikhonov(build_tweaks["tikhonov_gamma"])

        criterion = DudsonConvergence(build_tweaks["fbe_convergence_crit"])

        fbe_iterator = PicardDeltaIterator(
            eq,
            self.profiles,
            constraint_set,
            optimiser,
            plot=False,
            gif=False,
            relaxation=0.3,
            maxiter=400,
            convergence=criterion,
        )
        fbe_iterator()
        self._test_equilibrium_good(eq, psi_rtol=1e-3, li_rtol=1e-8)

        # Verify by removing symmetry constraint and checking convergence
        eq.force_symmetry = False
        eq.set_grid(grid)
        fbe_iterator()
        # I probably exported the eq before it was regridded without symmetry..
        self._test_equilibrium_good(eq, psi_rtol=1e-1, li_rtol=1e-4)

    def _test_equilibrium_good(self, eq, psi_rtol, li_rtol):
        lcfs_area = eq.get_LCFS().area
        assert np.isclose(self.eq_blueprint.get_LCFS().area, lcfs_area)

        li_bp = calc_li(self.eq_blueprint)
        assert np.isclose(li_bp, calc_li(eq), rtol=li_rtol)
        assert np.allclose(self.eq_blueprint.psi(), eq.psi(), rtol=psi_rtol)

    def _make_initial_psi(
        self,
        coilset,
        grid,
        constraint_set,
        x_current,
        z_current,
        plasma_current,
        tikhonov_gamma,
    ):
        coilset_temp = coilset.copy()
        dummy = Coil(
            x=x_current,
            z=z_current,
            dx=0,
            dz=0,
            current=plasma_current,
            name="plasma_dummy",
            control=False,
        )
        coilset_temp.add_coil(dummy)

        eq = Equilibrium(coilset_temp, grid, force_symmetry=True, psi=None, Ip=0)
        constraint_set(eq)
        optimiser = Norm2Tikhonov(tikhonov_gamma)
        currents = optimiser(eq, constraint_set)
        coilset_temp.set_control_currents(currents)
        # Note that this for some reason (incorrectly) only includes the psi from the
        # controlled coils and the plasma dummy psi contribution is not included...
        # which for some reason works better than with it.
        # proper mindfuck this... no idea why it wasn't working properly before, and
        # no idea why it works better with what is blatantly a worse starting solution.
        # Really you could just avoid adding the dummy plasma coil in the first place..
        # Perhaps the current centre is poorly estimated by R_0 + 0.5
        return coilset_temp.psi(grid.x, grid.z).copy() - dummy.psi(grid.x, grid.z)


if __name__ == "__main__":
    pytest.main([__file__])