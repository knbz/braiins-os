import logging
import subprocess
import shutil
import git
import os
import sys

from collections import OrderedDict
from termcolor import colored

from .config import RemoteWalker
from .repo import RepoProgressPrinter


class BuilderStop(Exception):
    """
    Exception raised when builder detected error and stopped immediately.
    """
    pass


class Builder:
    """
    Main class for building the Miner firmware based on the LEDE (OpenWRT) project.

    It prepares the LEDE source code and all related projects.
    Then it is possible to configure the project and build the firmware.
    The class also provides miscellaneous methods for cleaning build directories, firmware deployment and debugging
    on target platform.
    """
    LEDE = 'lede'
    LUCI = 'luci'
    LINUX = 'linux'
    CGMINER = 'cgminer'
    FEEDS_CONF_SRC = 'feeds.conf.default'
    FEEDS_CONF_DST = 'feeds.conf'
    CONFIG_NAME = '.config'

    def __init__(self, config):
        """
        Initialize builder for specific configuration

        :param config:
            Configuration object which has its attributes stored in dictionary or list.
            The key of dictionary can be also accessed as an object attribute.
        """
        self._config = config
        self._build_dir = os.path.join(os.path.abspath(config.build.dir), config.build.name)
        self._working_dir = None
        self._repos = OrderedDict()
        self._init_repos()

    def _run(self, *args, **kwargs):
        """
        Run system command in LEDE source directory

        The running environment is checked and when system command returns error it throws an exception.
        Two key arguments are supported. The `path` is for altering PATH environment variable and the `output`
        specifies if stdout is captured and returned by this method.

        :param args:
            First item is a command executed in the LEDE source directory.
            Remaining items are passed into the program as arguments.
            If args[0] is a list then this list is used instead of args.

            This allows use method in two forms:

            - `self._run([cmd, arg1, arg2])`
            - `self._run(cmd, arg1, arg2)`.
        :param kwargs:
            There are supported following key argument:

            - ``path`` - list of directories prepended to PATH environment variable
            - ``output`` - if true then method returns captured stdout otherwise stdout is printed to standard output
        :return:
            Captured stdout when `output` argument is set to True.
        """
        env = None
        cwd = self._working_dir
        path = kwargs.get('path')
        output = kwargs.get('output', False)
        stdout = subprocess.PIPE if output else None

        if path:
            env = os.environ.copy()
            env['PATH'] = ':'.join((*path, env['PATH']))
        if type(args[0]) is list:
            args = args[0]
        if path:
            logging.debug("Set PATH environment variable to '{}'".format(env['PATH']))

        logging.debug("Run '{}' in '{}'".format(' '.join(args), cwd))

        process = subprocess.run(args, stdout=stdout, check=True, cwd=cwd, env=env)
        if output:
            return process.stdout

    def _get_repo(self, name: str) -> git.Repo:
        """
        Return git repository by its name

        :param name: The name of repository as it has been specified in configuration file.
        :return: Associated git repository or raise exception if the repository does not exist.
        """
        return self._repos[name]

    def _get_repo_path(self, name: str) -> str:
        """
        Return absolute path to repository specified by its name

        :param name: The name of repository as it has been specified in configuration file.
        :return: Absolute path to the repository.
        """
        return os.path.join(self._build_dir, name)

    def _get_config_paths(self):
        """
        Return absolute paths to default and current configuration file

        - `default` configuration file points to a file specified in `build.config`
        - `current` configuration file points to a file in LEDE build directory

        :return:
            Pair of absolute paths to default and current configuration file.
        """
        lede_dir = self._working_dir
        config_src_path = os.path.abspath(self._config.build.config)
        config_dst_path = os.path.join(lede_dir, self.CONFIG_NAME)
        return config_src_path, config_dst_path

    def _init_repos(self):
        """
        Initialize all repositories specified in configuration file

        The list of repositories is stored under `remote.repos`.

        If repository is not cloned yet then None is used otherwise the repository is opened by `git.Repo`.
        """
        error = False
        for name in self._config.remote.repos:
            path = self._get_repo_path(name)
            logging.debug("Init repo '{}' in '{}'".format(name, path))
            repo = None
            try:
                repo = git.Repo(path)
            except git.exc.NoSuchPathError:
                logging.debug("Missing directory '{}'".format(path))
            except git.exc.InvalidGitRepositoryError:
                if os.listdir(path):
                    logging.error("Invalid Git repository '{}'".format(path))
                    error = True
                else:
                    logging.warning("Empty Git repository '{}'".format(path))
            self._repos[name] = repo
        if error:
            raise BuilderStop

    def _prepare_repo(self, remote):
        """
        Prepare one remote repository for use

        It clones or fetches latest changes from remote repository.
        The fetch can be altered by user in configuration file or from command line.
        When current branch differs from specified one it allow switching branches.

        :param remote:
            Named tuple where following attributes are used:

            - `name` - name of repository
            - `uri` - address of remote git repository
            - `branch` - name of branch
            - `fetch` - if True then fetch+merge is done
        """
        name = remote.name
        path = self._get_repo_path(name)
        repo = self._repos[name]
        logging.debug("Start preparing remote '{}' in '{}'".format(name, path))
        if not repo:
            logging.info("Cloning remote '{}'".format(name))
            repo = git.Repo.clone_from(remote.uri, path, branch=remote.branch,
                                       progress=RepoProgressPrinter())
            self._repos[name] = repo
        elif remote.fetch:
            logging.info("Fetching remote '{}'".format(name))
            for repo_remote in repo.remotes:
                repo_remote.fetch()
        if remote.branch not in repo.heads:
            for repo_remote in repo.remotes:
                if remote.branch in repo_remote.refs:
                    ref = repo_remote.refs[remote.branch]
                    repo.create_head(remote.branch, ref).set_tracking_branch(ref)
                    break
            else:
                logging.error("Branch '{}' does not exist".format(remote.branch))
                raise BuilderStop
        branch = repo.heads[remote.branch]
        if repo.active_branch != branch:
            branch.checkout()
        if remote.fetch:
            for repo_remote in repo.remotes:
                repo_remote.pull()

    def _prepare_feeds(self):
        """
        Prepare LEDE feeds

        It creates `feeds.conf` when it is not present and then calls

        - `./scripts/feeds update -a`
        - `./scripts/feeds install -a`
        """
        logging.info("Preparing feeds...")
        lede_dir = self._working_dir
        luci_dir = self._get_repo(self.LUCI).working_dir
        feeds_src_path = os.path.join(lede_dir, self.FEEDS_CONF_SRC)
        feeds_dst_path = os.path.join(lede_dir, self.FEEDS_CONF_DST)

        feeds_create = self._config.feeds.create_always == 'yes'
        feeds_update = self._config.feeds.update_always == 'yes'
        feeds_install = self._config.feeds.install_always == 'yes'

        if not os.path.exists(feeds_dst_path) or feeds_create:
            logging.debug("Creating '{}'".format(feeds_dst_path))
            feeds_update = True
            feeds_install = True
            with open(feeds_src_path, 'r') as feeds_src, open(feeds_dst_path, 'w') as feeds_dst:
                for line in feeds_src:
                    if self.LUCI not in line:
                        feeds_dst.write(line)
                # create link to LUCI in feeds configuration file
                feeds_dst.write('src-link {} {}\n'.format(self.LUCI, luci_dir))

        if feeds_update:
            logging.debug('Updating feeds')
            self._run(os.path.join('scripts', 'feeds'), 'update', '-a')
        if feeds_install:
            logging.debug('Installing feeds')
            self._run(os.path.join('scripts', 'feeds'), 'install', '-a')

    def _prepare_config(self):
        """
        Prepare LEDE configuration file

        It sets default configuration specified in the configuration file under `build.config`.
        It also sets paths to Linux and CGMiner external directories in this configuration file.
        """
        logging.info("Preparing config...")
        linux_dir = self._get_repo(self.LINUX).working_dir
        cgminer_dir = self._get_repo(self.CGMINER).working_dir
        config_src_path, config_dst_path = self._get_config_paths()

        config_copy = self._config.build.config_always == 'yes'
        default_config = not os.path.exists(config_dst_path)

        if default_config:
            logging.debug("Creating default configuration")
            self._run('make', 'defconfig')

        config_src_time = os.path.getmtime(config_src_path)
        config_dst_time = os.path.getmtime(config_dst_path)
        if default_config or (config_dst_time < config_src_time) or config_copy:
            logging.debug("Copy config from '{}'".format(config_src_path))
            shutil.copy(config_src_path, config_dst_path)
            logging.debug("Set external kernel tree to '{}'".format(linux_dir))
            logging.debug("Set external CGMiner tree to '{}'".format(cgminer_dir))
            with open(config_dst_path, 'a') as config_dst:
                # set paths to Linux and CGMiner external directories
                config_dst.write('CONFIG_EXTERNAL_KERNEL_TREE="{}"\n'.format(linux_dir))
                config_dst.write('CONFIG_EXTERNAL_CGMINER_TREE="{}"\n'.format(cgminer_dir))
            logging.debug("Creating full configuration file")
            self._run('make', 'defconfig')

    def _config_lede(self):
        """
        Configure LEDE project

        It calls `make menuconfig` and then stores configuration diff to the file specified in `build.config`.
        """
        config_dst_path, config_src_path = self._get_config_paths()

        config_src_time = os.path.getmtime(config_src_path)
        self._run('make', 'menuconfig')
        if os.path.getmtime(config_src_path) == config_src_time:
            logging.info("Configuration file has not been changed")
            return

        logging.info("Saving changes in configuration to '{}'...".format(config_dst_path))
        with open(config_dst_path, 'w') as config_dst:
            configs = ['CONFIG_EXTERNAL_KERNEL_TREE', 'CONFIG_EXTERNAL_CGMINER_TREE']
            # call ./scripts/diffconfig.sh to get configuration diff
            output = self._run(os.path.join('scripts', 'diffconfig.sh'), output=True)
            for line in output.decode('utf-8').splitlines():
                # do not store lines with configuration of external directories
                # this files are automatically generated
                if not any(line.startswith(config) for config in configs):
                    config_dst.write(line)
                    config_dst.write('\n')

    def _config_kernel(self):
        """
        Configure Linux kernel

        It calls `make kernel_menuconfig`. The configuration is stored in the target directory of the LEDE build system.
        """
        self._run('make', 'kernel_menuconfig')

    def prepare(self, fetch: bool=False):
        """
        Prepare all projects and configure the LEDE build system.

        :param fetch:
            If True then override configuration file and force fetch all repositories.
        """
        logging.info("Preparing build directory...'")
        if not os.path.exists(self._build_dir):
            logging.debug("Creating build directory '{}'".format(self._build_dir))
            os.makedirs(self._build_dir)
        for remote in RemoteWalker(self._config.remote, fetch):
            self._prepare_repo(remote)

        # set working directory to LEDE root directory
        self._working_dir = self._get_repo(self.LEDE).working_dir

        self._prepare_feeds()
        self._prepare_config()

    def clean(self, purge: bool=False):
        """
        Clean all projects or purge them to initial state.

        :param purge:
            If True then use git to clean the whole repository to its initial state.
        """
        logging.info("Start cleaning LEDE build directory...'")
        if not purge:
            self._run('make', 'clean')
        else:
            for name, repo in self._repos.items():
                if not repo:
                    continue
                logging.debug("Purging '{}'".format(name))
                repo.git.clean('-dxf')

    def config(self, kernel: bool=False):
        """
        Configure LEDE project or Linux kernel

        :param kernel:
            If True then Linux kernel configuration is called instead of LEDE configuration.
        """
        if not kernel:
            logging.info("Start LEDE configuration...'")
            self._config_lede()
        else:
            logging.info("Start Linux kernel configuration...'")
            self._config_kernel()

    def build(self):
        """
        Build the Miner firmware for current configuration

        It is possible alter build system by following attributes in configuration file:

        - `build.jobs` - number of jobs to run simultaneously (default is `1`)
        - `build.debug` - show all commands during build process (default is `no`)
        """
        logging.info("Start building LEDE...'")
        jobs = self._config.build.get('jobs', 1)
        debug = self._config.build.get('debug', 'no') == 'yes'
        xilinx_sdk = os.path.abspath(os.path.expanduser(self._config.build.xilinx_sdk))
        xilinx_bin = os.path.join(xilinx_sdk, 'bin')

        # prepare arguments for build
        args = ['make', '-j{}'.format(jobs)]
        if debug:
            args.append('V=s')
        # run make to build whole LEDE
        self._run(args, path=[xilinx_bin])

    def deploy(self):
        """
        Deploy Miner firmware to target platform
        """
        pass

    def status(self):
        """
        Show status of all repositories

        It is equivalent of `git status` and shows all changes in related projects.
        """
        def get_diff_path(diff):
            if diff.change_type[0] == 'R':
                return '{} -> {}'.format(diff.a_path, diff.b_path)
            else:
                return diff.a_path

        for name, repo in self._repos.items():
            logging.info("Status for '{}' ({})".format(name, repo.active_branch.name))
            clean = True
            indexed_files = repo.head.commit.diff()
            if len(indexed_files):
                print('Changes to be committed:')
                for indexed_file in indexed_files:
                    change_type = indexed_file.change_type[0]
                    print('\t{}'.format(change_type), colored(get_diff_path(indexed_file), 'green'))
                print()
                clean = False
            staged_files = repo.index.diff(None)
            if len(staged_files):
                print('Changes not staged for commit:')
                for staged_file in staged_files:
                    change_type = staged_file.change_type[0]
                    print('\t{}'.format(change_type), colored(get_diff_path(staged_file), 'red'))
                print()
                clean = False
            if len(repo.untracked_files):
                print('Untracked files:')
                for untracked_file in repo.untracked_files:
                    print(colored('\t{}'.format(untracked_file), 'red'))
                print()
                clean = False
            if clean:
                print('nothing to commit, working tree clean')
                print()

    def debug(self):
        """
        Remotely run program on target platform and attach debugger to it
        """
        pass

    def toolchain(self):
        """
        Prepare environment for LEDE toolchain

        The bash script is returned to the stdout which can be then evaluated in parent process to correctly set build
        environment for LEDE toolchain. It is then possible to use gcc and other tools from this SDK in external
        projects.
        """
        logging.info("Preparing toolchain environment...'")
        staging_dir = os.path.join(self._working_dir, 'staging_dir')
        target_dir = os.path.join(staging_dir, 'target-arm_cortex-a9+neon_musl-1.1.16_eabi')
        toolchain_dir = os.path.join(staging_dir, 'toolchain-arm_cortex-a9+neon_gcc-5.4.0_musl-1.1.16_eabi')

        if not os.path.exists(target_dir):
            msg = "Target directory '{}' does not exist".format(target_dir)
            logging.error(msg)
            sys.stdout.write('echo {};\n'.format(msg))
            raise BuilderStop

        if not os.path.exists(toolchain_dir):
            msg = "Toolchain directory '{}' does not exist".format(toolchain_dir)
            logging.error(msg)
            sys.stdout.write('echo {};\n'.format(msg))
            raise BuilderStop

        env_path = os.environ.get('PATH', '')

        sys.stderr.write('# set environment with command:\n')
        sys.stderr.write('# eval $(./lede.py toolchain 2>/dev/null)\n')
        sys.stdout.write('TARGET="{}";\n'.format(target_dir))
        sys.stdout.write('TOOLCHAIN="{}";\n'.format(toolchain_dir))
        sys.stdout.write('export STAGING_DIR="${TARGET}";\n')

        if (toolchain_dir + '/bin') not in env_path:
            # export PATH only if it has not been exported already
            sys.stdout.write('export PATH="${TOOLCHAIN}/bin:$PATH";\n')