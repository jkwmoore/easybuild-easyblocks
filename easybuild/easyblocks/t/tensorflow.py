##
# Copyright 2009-2017 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
EasyBuild support for building and installing TensorFlow, implemented as an easyblock
"""
import glob
import os
import stat
import tempfile

import easybuild.tools.environment as env
import easybuild.tools.toolchain as toolchain
from easybuild.easyblocks.generic.pythonpackage import PythonPackage
from easybuild.framework.easyconfig import CUSTOM
from easybuild.tools.build_log import EasyBuildError
from easybuild.tools.filetools import adjust_permissions, apply_regex_substitutions, mkdir, resolve_path
from easybuild.tools.filetools import which, write_file
from easybuild.tools.modules import get_software_root, get_software_version
from easybuild.tools.run import run_cmd


INTEL_COMPILER_WRAPPER = """#!/bin/bash
export INTEL_LICENSE_FILE='%(intel_license_file)s'
export CPATH='%(cpath)s'
%(compiler_path)s "$@"
"""


class EB_TensorFlow(PythonPackage):
    """Support for building/installing TensorFlow."""

    @staticmethod
    def extra_options():
        extra_vars = {
            # see https://developer.nvidia.com/cuda-gpus
            'cuda_compute_capabilities': [[], "List of CUDA compute capabilities to build with", CUSTOM],
            'with_mkl_dnn': [True, "Make TensorFlow use Intel MKL-DNN", CUSTOM],
        }
        return PythonPackage.extra_options(extra_vars)

    def configure_step(self):
        """Custom configuration procedure for TensorFlow."""

        tmpdir = tempfile.mkdtemp(suffix='-bazel-configure')

        # Bazel reset environment in which build is performed, so $INTEL_LICENSE_FILE gets unset
        # so, we create a wrapper for icc to make sure location of license server is available...
        # cfr. https://github.com/bazelbuild/bazel/issues/663
        if self.toolchain.comp_family() == toolchain.INTELCOMP:
            icc_wrapper_txt = INTEL_COMPILER_WRAPPER % {
                'compiler_path': which('icc'),
                'intel_license_file': os.getenv('INTEL_LICENSE_FILE', os.getenv('LM_LICENSE_FILE')),
                'cpath': os.getenv('CPATH'),
            }
            icc_wrapper = os.path.join(tmpdir, 'bin', 'icc')
            write_file(icc_wrapper, icc_wrapper_txt)
            adjust_permissions(icc_wrapper, stat.S_IXUSR)
            env.setvar('PATH', ':'.join([os.path.dirname(icc_wrapper), os.getenv('PATH')]))
            self.log.info("Using wrapper script for 'icc': %s", which('icc'))

        self.prepare_python()

        cuda_root = get_software_root('CUDA')
        cudnn_root = get_software_root('cuDNN')
        jemalloc_root = get_software_root('jemalloc')
        opencl_root = get_software_root('OpenCL')

        use_mpi = self.toolchain.options.get('usempi', False)

        config_env_vars = {
            'CC_OPT_FLAGS': os.getenv('CXXFLAGS'),
            'MPI_HOME': '',
            'PYTHON_BIN_PATH': self.python_cmd,
            'PYTHON_LIB_PATH': os.path.join(self.installdir, self.pylibdir),
            'TF_CUDA_CLANG': '0',
            'TF_ENABLE_XLA': '0',  # XLA JIT support
            'TF_NEED_CUDA': ('0', '1')[bool(cuda_root)],
            'TF_NEED_GCP': '0',  # Google Cloud Platform
            'TF_NEED_GDR': '0',
            'TF_NEED_HDFS': '0',  # Hadoop File System
            'TF_NEED_JEMALLOC': ('0', '1')[bool(jemalloc_root)],
            'TF_NEED_MPI': ('0', '1')[bool(use_mpi)],
            'TF_NEED_OPENCL': ('0', '1')[bool(opencl_root)],
            'TF_NEED_S3': '0',  # Amazon S3 File System
            'TF_NEED_VERBS': '0',
        }
        if cuda_root:
            config_env_vars.update({
                'CUDA_TOOLKIT_PATH': cuda_root,
                'GCC_HOST_COMPILER_PATH': which(os.getenv('CC')),
                'TF_CUDA_COMPUTE_CAPABILITIES': ','.join(self.cfg['cuda_compute_capabilities']),
                'TF_CUDA_VERSION': get_software_version('CUDA'),
            })
        if cudnn_root:
            config_env_vars.update({
                'CUDNN_INSTALL_PATH': cudnn_root,
                'TF_CUDNN_VERSION': get_software_version('cuDNN'),
            })

        for (key, val) in sorted(config_env_vars.items()):
            env.setvar(key, val)

        # patch configure.py (called by configure script) to avoid that Bazel abuses $HOME/.cache/bazel
        regex_subs = [(r"(run_shell\(\['bazel')", r"\1, '--output_base=%s'" % tmpdir)]
        apply_regex_substitutions('configure.py', regex_subs)

        run_cmd('./configure', log_all=True, simple=True)

    def build_step(self):
        """Custom build procedure for TensorFlow."""

        # pre-create target installation directory
        mkdir(os.path.join(self.installdir, self.pylibdir), parents=True)

        # patch all CROSSTOOL* scripts to fix hardcoding of locations of binutils/GCC binaries
        binutils_root = get_software_root('binutils')
        if binutils_root:
            binutils_bin = os.path.join(binutils_root, 'bin')
        else:
            raise EasyBuildError("Failed to determine installation prefix for binutils")

        gcc_root = get_software_root('GCCcore') or get_software_root('GCC')
        if gcc_root:

            gcc_lib64 = os.path.join(gcc_root, 'lib64')

            gcc_ver = get_software_version('GCCcore') or get_software_version('GCC')
            res = glob.glob(os.path.join(gcc_root, 'lib', 'gcc', '*', gcc_ver, 'include'))
            if res and len(res) == 1:
                gcc_lib_inc = res[0]
            else:
                raise EasyBuildError("Failed to pinpoint location of GCC include files: %s", res)

            gcc_lib_inc_fixed = os.path.join(os.path.dirname(gcc_lib_inc), 'include-fixed')
            if not os.path.exists(gcc_lib_inc_fixed):
                raise EasyBuildError("Derived directory %s does not exist", gcc_lib_inc_fixed)

            gcc_cplusplus_inc = os.path.join(gcc_root, 'include', 'c++', gcc_ver)
            if not os.path.exists(gcc_cplusplus_inc):
                raise EasyBuildError("Derived directory %s does not exist", gcc_cplusplus_inc)
        else:
            raise EasyBuildError("Failed to determine installation prefix for GCC")

        inc_paths = [gcc_lib_inc, gcc_lib_inc_fixed, gcc_cplusplus_inc]
        lib_paths = [gcc_lib64]

        cuda_root = get_software_root('CUDA')
        if cuda_root:
            inc_paths.append(os.path.join(cuda_root, 'include'))
            lib_paths.append(os.path.join(cuda_root, 'lib64'))

        regex_subs = [
            (r'-B/usr/bin/', '-B%s/ %s' % (binutils_bin, ' '.join('-L%s/' % p for p in lib_paths))),
            (r'(cxx_builtin_include_directory:).*', ''),
            (r'^toolchain {', 'toolchain {\n' + '\n'.join(r'cxx_builtin_include_directory: "%s"' % resolve_path(p) for p in inc_paths)),
        ]
        for tool in ['ar', 'cpp', 'dwp', 'gcc', 'gcov', 'ld', 'nm', 'objcopy', 'objdump', 'strip']:
            path = which(tool)
            if path:
                regex_subs.append((os.path.join('/usr', 'bin', tool), path))
            else:
                raise EasyBuildError("Failed to determine path to '%s'", tool)

        if self.toolchain.options.get('pic', None):
            # -fPIE/-pie and -fPIC are not compatible, so patch out hardcoded occurences of -fPIE & -pie
            regex_subs.extend([('-fPIE', '-fPIC'), ('"-pie"', '"-fPIC"')])

        for path, dirnames, filenames in os.walk(self.start_dir):
            for filename in filenames:
                if filename.startswith('CROSSTOOL'):
                    full_path = os.path.join(path, filename)
                    self.log.info("Patching %s", full_path)
                    apply_regex_substitutions(full_path, regex_subs)

        tmpdir = tempfile.mkdtemp(suffix='-bazel-build')

        cmd = ['bazel', '--output_base=%s' % tmpdir, 'build']
        # https://docs.bazel.build/versions/master/user-manual.html#flag--compilation_mode
        cmd.append('--compilation_mode=opt')
        # https://docs.bazel.build/versions/master/user-manual.html#flag--config
        cmd.append('--config=opt')
        # https://docs.bazel.build/versions/master/user-manual.html#flag--subcommands
        # https://docs.bazel.build/versions/master/user-manual.html#flag--verbose_failures
        cmd.extend(['--subcommands', '--verbose_failures'])

        if self.toolchain.options.get('pic', None):
            cmd.append('--copt="-fPIC"')

        cmd.append(self.cfg['buildopts'])

        if cuda_root:
            cmd.append('--config=cuda')

        if self.cfg['with_mkl_dnn']:
            # this makes TensorFlow download & use mkl-dnn (cfr. https://github.com/01org/mkl-dnn)
            # using the full Intel MKL doesn't work without additional effort...
            cmd.extend(['--config=mkl'])

        cmd.append('//tensorflow/tools/pip_package:build_pip_package')

        run_cmd(' '.join(cmd), log_all=True, simple=True, log_ok=True)

        cmd = "bazel-bin/tensorflow/tools/pip_package/build_pip_package %s" % self.builddir
        run_cmd(cmd, log_all=True, simple=True, log_ok=True)

    def test_step(self):
        """No (reliable) custom test procedure for TensorFlow."""
        pass

    def install_step(self):
        """Custom install procedure for TensorFlow."""
        whl_paths = glob.glob(os.path.join(self.builddir, 'tensorflow-%s-*.whl' % self.version))
        if len(whl_paths) == 1:
            # --upgrade is required to ensure *this* wheel is installed
            # cfr. https://github.com/tensorflow/tensorflow/issues/7449
            cmd = "pip install --ignore-installed --prefix=%s %s" % (self.installdir, whl_paths[0])
            run_cmd(cmd, log_all=True, simple=True, log_ok=True)
        else:
            raise EasyBuildError("Failed to isolate built .whl in %s: %s", whl_paths, self.builddir)

        # test installation using MNIST tutorial examples
        # (can't be done in sanity check because mnist_deep.py is not part of installation)
        if self.cfg['runtest']:
            pythonpath = os.getenv('PYTHONPATH', '')
            env.setvar('PYTHONPATH', '%s:%s' % (os.path.join(self.installdir, self.pylibdir), pythonpath))

            for mnist_py in ['mnist_softmax.py', 'mnist_with_summaries.py']:
                tmpdir = tempfile.mkdtemp(suffix='-tf-%s-test' % os.path.splitext(mnist_py)[0])
                mnist_py = os.path.join(self.start_dir, 'tensorflow', 'examples', 'tutorials', 'mnist', mnist_py)
                cmd = "%s %s --data_dir %s" % (self.python_cmd, mnist_py, tmpdir)
                run_cmd(cmd, log_all=True, simple=True, log_ok=True)

    def sanity_check_step(self):
        """Custom sanity check for TensorFlow."""
        custom_paths = {
            'files': ['bin/tensorboard'],
            'dirs': [self.pylibdir],
        }

        custom_commands = [
            "%s -c 'import tensorflow'" % self.python_cmd,
            # tf_should_use importsweakref.finalize, which requires backports.weakref for Python < 3.4
            "%s -c 'from tensorflow.python.util import tf_should_use'" % self.python_cmd,
        ]
        super(EB_TensorFlow, self).sanity_check_step(custom_paths=custom_paths, custom_commands=custom_commands)
