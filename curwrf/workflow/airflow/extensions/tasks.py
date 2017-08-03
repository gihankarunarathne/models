import json
import logging
import os

from airflow.models import Variable
from curwrf.wrf import utils
from curwrf.wrf.execution import executor
from curwrf.wrf.execution.executor import WrfConfig


class CurwTask(object):
    def __init__(self):
        self.config = None

    def pre_process(self, *args, **kwargs):
        pass

    def post_process(self, *args, **kwargs):
        pass

    def process(self, *args, **kwargs):
        pass

    def get_config(self, **kwargs):
        if self.config is None:
            self.set_config(**kwargs)
        return self.config

    def set_config(self, **kwargs):
        raise NotImplementedError('Provide a way to get the config!')


class WrfTask(CurwTask):
    def __init__(self):
        super(WrfTask, self).__init__()

    def set_config(self, **kwargs):
        if self.config is None:
            if 'ti' in kwargs:
                wrf_config_json = kwargs['ti'].xcom_pull(task_ids=None, key='wrf_config_json')
                logging.info('wrf_config from xcom: ' + wrf_config_json)
                self.config = WrfConfig(json.loads(wrf_config_json))
            else:
                try:
                    self.config = WrfConfig(Variable.get('wrf_config', deserialize_json=True))
                except KeyError:
                    raise CurwAriflowTasksException('Unable to find WrfConfig')


class Ungrib(WrfTask):
    def pre_process(self, *args, **kwargs):
        logging.info('Running preprocessing for ungrib...')

        wrf_config = self.get_config(**kwargs)

        wps_dir = utils.get_wps_dir(wrf_config.get('wrf_home'))
        logging.info('WPS dir: %s' % wps_dir)

        logging.info('Cleaning up files')
        utils.delete_files_with_prefix(wps_dir, 'FILE:*')
        utils.delete_files_with_prefix(wps_dir, 'PFILE:*')

        logging.info('Replacing namellist.wps place-holders')
        executor.replace_namelist_wps(wrf_config)

        # Linking VTable
        if not os.path.exists(os.path.join(wps_dir, 'Vtable')):
            logging.info('Creating Vtable symlink')
            os.symlink(os.path.join(wps_dir, 'ungrib/Variable_Tables/Vtable.NAM'), os.path.join(wps_dir, 'Vtable'))
        pass

    def process(self, *args, **kwargs):
        logging.info('Running ungrib...')

        wrf_config = self.get_config(**kwargs)
        wps_dir = utils.get_wps_dir(wrf_config.get('wrf_home'))

        # Running link_grib.csh
        logging.info('Running link_grib.csh')
        gfs_date, gfs_cycle, start = utils.get_appropriate_gfs_inventory(wrf_config)
        # use get_gfs_data_url_dest_tuple to get
        dest = \
            utils.get_gfs_data_url_dest_tuple(wrf_config.get('gfs_url'), wrf_config.get('gfs_inv'), gfs_date, gfs_cycle,
                                              '', wrf_config.get('gfs_res'), '')[1]

        utils.run_subprocess(
            'csh link_grib.csh %s/%s' % (wrf_config.get('gfs_dir'), dest), cwd=wps_dir)

        utils.run_subprocess('./ungrib.exe', cwd=wps_dir)


class Metgrid(WrfTask):
    def __init__(self):
        super(Metgrid, self).__init__()

    def pre_process(self, *args, **kwargs):
        logging.info('Running preporcessing for metgrid...')

        wrf_config = self.get_config(**kwargs)
        wps_dir = utils.get_wps_dir(wrf_config.get('wrf_home'))
        utils.delete_files_with_prefix(wps_dir, 'met_em*')

        logging.info('Replacing namellist.wps place-holders')
        executor.replace_namelist_wps(wrf_config)

    def process(self, *args, **kwargs):
        logging.info('Running metgrid...')
        wrf_config = self.get_config(**kwargs)
        wps_dir = utils.get_wps_dir(wrf_config.get('wrf_home'))

        utils.run_subprocess('./metgrid.exe', cwd=wps_dir)


class Geogrid(WrfTask):
    def __init__(self):
        super(Geogrid, self).__init__()

    def process(self, *args, **kwargs):
        logging.info('Running geogrid...')

        wrf_config = self.get_config(**kwargs)
        wps_dir = utils.get_wps_dir(wrf_config.get('wrf_home'))

        logging.info('Replacing namellist.wps place-holders')
        executor.replace_namelist_wps(wrf_config)

        if not executor.check_geogrid_output(wps_dir):
            logging.info('Running Geogrid.exe')
            utils.run_subprocess('./geogrid.exe', cwd=wps_dir)
        else:
            logging.info('Geogrid output already available')


class Real(WrfTask):
    def pre_process(self, *args, **kwargs):
        wrf_home = self.get_config(**kwargs).get('wrf_home')

        logging.info('Replacing namelist.input place-holders')
        executor.replace_namelist_input(self.get_config(**kwargs))

        logging.info('Running em_real...')
        em_real_dir = utils.get_em_real_dir(wrf_home)

        logging.info('Cleaning up files')
        utils.delete_files_with_prefix(em_real_dir, 'met_em*')
        utils.delete_files_with_prefix(em_real_dir, 'rsl*')

        # Linking met_em.*
        logging.info('Creating met_em.d* symlinks')
        utils.create_symlink_with_prefix(utils.get_wps_dir(wrf_home), 'met_em.d*', em_real_dir)

    def process(self, *args, **kwargs):
        wrf_home = self.get_config(**kwargs).get('wrf_home')
        em_real_dir = utils.get_em_real_dir(wrf_home)
        procs = self.get_config(**kwargs).get('procs')
        utils.run_subprocess('mpirun -np %d ./real.exe' % procs, cwd=em_real_dir)

    def post_process(self, *args, **kwargs):
        wrf_home = self.get_config(**kwargs).get('wrf_home')
        start_date = self.get_config(**kwargs).get('start_date')
        em_real_dir = utils.get_em_real_dir(wrf_home)

        logging.info('Moving the real logs')
        utils.move_files_with_prefix(em_real_dir, 'rsl*', os.path.join(utils.get_logs_dir(wrf_home),
                                                                       'rsl-real-%s' % start_date))


class Wrf(WrfTask):
    def process(self, *args, **kwargs):
        wrf_home = self.get_config(**kwargs).get('wrf_home')
        em_real_dir = utils.get_em_real_dir(wrf_home)
        procs = self.get_config(**kwargs).get('procs')

        logging.info('Replacing namelist.input place-holders')
        executor.replace_namelist_input(self.get_config(**kwargs))

        utils.run_subprocess('mpirun -np %d ./wrf.exe' % procs, cwd=em_real_dir)

    def post_process(self, *args, **kwargs):
        wrf_home = self.get_config(**kwargs).get('wrf_home')
        em_real_dir = utils.get_em_real_dir(wrf_home)
        start_date = self.get_config(**kwargs).get('start_date')

        logging.info('Moving the WRF logs')
        utils.move_files_with_prefix(em_real_dir, 'rsl*',
                                     os.path.join(utils.get_logs_dir(wrf_home), 'rsl-wrf-%s' % start_date))

        logging.info('Moving the WRF files to output directory')
        utils.move_files_with_prefix(utils.get_em_real_dir(wrf_home), 'wrfout_d*', utils.get_output_dir(wrf_home))


class CurwAriflowTasksException(Exception):
    def __init__(self, msg):
        self.msg = msg
        Exception.__init__(self, msg)