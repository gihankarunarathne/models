import csv
import datetime as dt
import logging
import multiprocessing
import os
import shutil
import unittest
import zipfile
from random import random

import numpy as np
import pandas as pd
import shapefile
from joblib import Parallel, delayed
from mpl_toolkits.basemap import Basemap, cm
from netCDF4 import Dataset
from tempfile import TemporaryDirectory

from curw.rainfall.wrf import utils
from curw.rainfall.wrf.extraction import constants
from curw.rainfall.wrf.resources import manager as res_mgr
from curw.rainfall.wrf.extraction import utils as ext_utils
from curwmysqladapter import Station


def extract_time_data(nc_f):
    nc_fid = Dataset(nc_f, 'r')
    times_len = len(nc_fid.dimensions['Time'])
    try:
        times = [''.join(x) for x in nc_fid.variables['Times'][0:times_len]]
    except TypeError:
        times = np.array([''.join([y.decode() for y in x]) for x in nc_fid.variables['Times'][:]])
    nc_fid.close()
    return times_len, times


def extract_metro_colombo(nc_f, wrf_output, wrf_output_base, curw_db_adapter=None, curw_db_upsert=False,
                          run_prefix='WRF',
                          run_name='Cloud-1'):
    """
    extract Metro-Colombo rf and divide area into to 4 quadrants 
    :param wrf_output_base: 
    :param run_name: 
    :param nc_f: 
    :param wrf_output: 
    :param curw_db_adapter: If not none, data will be pushed to the db 
    :param run_prefix: 
    :param curw_db_upsert: 
    :return: 
    """
    prefix = 'met_col'
    lon_min, lat_min, lon_max, lat_max = constants.COLOMBO_EXTENT

    nc_vars = ext_utils.extract_variables(nc_f, ['RAINC', 'RAINNC'], lat_min, lat_max, lon_min, lon_max)
    lats = nc_vars['XLAT']
    lons = nc_vars['XLONG']
    prcp = nc_vars['RAINC'] + nc_vars['RAINNC']
    times = nc_vars['Times']

    diff = prcp[1:len(times), :, :] - prcp[0:len(times) - 1, :, :]

    width = len(lons)
    height = len(lats)

    output_dir = utils.create_dir_if_not_exists(os.path.join(wrf_output, prefix))

    basin_rf = np.mean(diff[0:(len(times) - 1 if len(times) < 24 else 24), :, :])

    alpha_file_path = os.path.join(wrf_output_base, prefix + '_alphas.txt')
    utils.create_dir_if_not_exists(os.path.dirname(alpha_file_path))
    with open(alpha_file_path, 'a+') as alpha_file:
        t = utils.datetime_utc_to_lk(dt.datetime.strptime(times[0], '%Y-%m-%d_%H:%M:%S'))
        alpha_file.write('%s %f\n' % (t.strftime('%Y-%m-%d_%H:%M:%S'), basin_rf))

    cz = ext_utils.get_mean_cell_size(lats, lons)
    no_data = -99

    divs = (2, 2)
    div_rf = {}
    for i in range(divs[0] * divs[1]):
        div_rf[prefix + str(i)] = []

    with TemporaryDirectory(prefix=prefix) as temp_dir:
        subsection_file_path = os.path.join(temp_dir, 'sub_means.txt')
        with open(subsection_file_path, 'w') as subsection_file:
            for tm in range(0, len(times) - 1):
                t_str = (utils.datetime_utc_to_lk(dt.datetime.strptime(times[tm], '%Y-%m-%d_%H:%M:%S'))).strftime(
                    '%Y-%m-%d %H:%M:%S')

                output_file_path = os.path.join(temp_dir, 'rf_' + t_str.replace(' ', '_') + '.asc')
                ext_utils.create_asc_file(np.flip(diff[tm], 0), lats, lons, output_file_path, cell_size=cz,
                                          no_data_val=no_data)

                # writing subsection file
                x_idx = [round(i * width / divs[0]) for i in range(0, divs[0] + 1)]
                y_idx = [round(i * height / divs[1]) for i in range(0, divs[1] + 1)]

                subsection_file.write(t_str)
                for j in range(len(y_idx) - 1):
                    for i in range(len(x_idx) - 1):
                        quad = j * divs[1] + i
                        sub_sec_mean = np.mean(diff[tm, y_idx[j]:y_idx[j + 1], x_idx[i]: x_idx[i + 1]])
                        subsection_file.write(' %f' % sub_sec_mean)
                        div_rf[prefix + str(quad)].append([t_str, sub_sec_mean])
                subsection_file.write('\n')

        utils.create_zip_with_prefix(temp_dir, 'rf_*.asc', os.path.join(temp_dir, 'ascs.zip'), clean_up=True)

        utils.move_files_with_prefix(temp_dir, '*', output_dir)

    # writing to the database
    if curw_db_adapter is not None:
        for i in range(divs[0] * divs[1]):
            name = prefix + str(i)
            station = [Station.CUrW, name, name, -999, -999, 0, "met col quadrant %d" % i]
            if ext_utils.create_station_if_not_exists(curw_db_adapter, station):
                logging.info('%s station created' % name)

        logging.info('Pushing data to the db...')
        ext_utils.push_rainfall_to_db(curw_db_adapter, div_rf, upsert=curw_db_upsert, source=run_prefix, name=run_name)

    return basin_rf


def test_extract_metro_colombo():
    adapter = ext_utils.get_curw_adapter()
    extract_metro_colombo('/home/curw/Desktop/wrfout_d03_2017-07-31_00:00:00',
                          dt.datetime.strptime('2017-07-31', '%Y-%m-%d'), '/home/curw/temp/', curw_db_adapter=adapter)


def extract_weather_stations(nc_f, wrf_output, weather_stations=None, curw_db_adapter=None, curw_db_upsert=False,
                             run_prefix='WRF', run_name='Cloud-1'):
    # todo: change this to use lat lons!
    if weather_stations is None:
        weather_stations = res_mgr.get_resource_path('extraction/local/kelani_basin_stations.txt')

    nc_fid = Dataset(nc_f, 'r')
    times_len, times = extract_time_data(nc_f)

    prefix = 'stations_rf'
    stations_dir = utils.create_dir_if_not_exists(os.path.join(wrf_output, prefix))

    stations_rf = {}
    with TemporaryDirectory(prefix=prefix) as temp_dir:
        with open(weather_stations, 'r') as csvfile:
            stations = csv.reader(csvfile, delimiter=' ')

            for row in stations:
                logging.info(' '.join(row))
                lon = row[1]
                lat = row[2]

                station_prcp = nc_fid.variables['RAINC'][:, lat, lon] + nc_fid.variables['RAINNC'][:, lat, lon]

                station_diff = station_prcp[1:len(times)] - station_prcp[0:len(times) - 1]

                stations_rf[row[0]] = []

                station_file_path = os.path.join(temp_dir, row[0] + '_%s.txt' % prefix)
                with open(station_file_path, 'w') as station_file:
                    for t in range(0, len(times) - 1):
                        t_str = (
                            utils.datetime_utc_to_lk(dt.datetime.strptime(times[t], '%Y-%m-%d_%H:%M:%S'))).strftime(
                            '%Y-%m-%d %H:%M:%S')
                        station_file.write('%s %f\n' % (t_str, station_diff[t]))
                        stations_rf[row[0]].append([t_str, station_diff[t]])

        utils.move_files_with_prefix(temp_dir, '*.txt', stations_dir)

    if curw_db_adapter is not None:
        logging.info('Pushing data to the db...')
        ext_utils.push_rainfall_to_db(curw_db_adapter, stations_rf, upsert=curw_db_upsert, name=run_name,
                                      source=run_prefix)

    nc_fid.close()


def test_extract_weather_stations():
    adapter = ext_utils.get_curw_adapter()
    extract_weather_stations('/home/curw/Desktop/wrfout_d03_2017-07-31_00:00:00',
                             dt.datetime.strptime('2017-07-31', '%Y-%m-%d'), '/home/curw/temp/',
                             curw_db_adapter=adapter)


# todo: update this!!!
def extract_kelani_basin_rainfall(nc_f, date, wrf_output, avg_basin_rf=1.0, kelani_basin_file=None):
    if kelani_basin_file is None:
        kelani_basin_file = res_mgr.get_resource_path('extraction/local/kelani_basin_points_250m.txt')

    points = np.genfromtxt(kelani_basin_file, delimiter=',')

    kel_lon_min = np.min(points, 0)[1]
    kel_lat_min = np.min(points, 0)[2]
    kel_lon_max = np.max(points, 0)[1]
    kel_lat_max = np.max(points, 0)[2]

    diff, kel_lats, kel_lons, times = extract_area_rf_series(nc_f, kel_lat_min, kel_lat_max, kel_lon_min, kel_lon_max)

    def get_bins(arr):
        sz = len(arr)
        return (arr[1:sz - 1] + arr[0:sz - 2]) / 2

    lat_bins = get_bins(kel_lats)
    lon_bins = get_bins(kel_lons)

    output_dir = wrf_output + '/kelani-basin/created-' + date.strftime('%Y-%m-%d')
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    prev_day_1_file = wrf_output + '/wrfout_d03_' + (date - dt.timedelta(days=1)).strftime('%Y-%m-%d') + '_00:00:00'
    prev_day_2_file = wrf_output + '/wrfout_d03_' + (date - dt.timedelta(days=2)).strftime('%Y-%m-%d') + '_00:00:00'

    diff1, _, _, times1 = extract_area_rf_series(prev_day_1_file, kel_lat_min, kel_lat_max, kel_lon_min, kel_lon_max)
    diff2, _, _, times2 = extract_area_rf_series(prev_day_2_file, kel_lat_min, kel_lat_max, kel_lon_min, kel_lon_max)

    def write_forecast_to_raincell_file(output_file_path, alpha):
        output_file = open(output_file_path, 'w')

        res = 60
        data_hours = len(times) + 48
        start_ts = (date - dt.timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        end_ts = (date + dt.timedelta(hours=len(times) - 1)).strftime('%Y-%m-%d %H:%M:%S')
        output_file.write("%d %d %s %s\n" % (res, data_hours, start_ts, end_ts))

        for h in range(0, data_hours):
            for point in points:
                rf_x = np.digitize(point[1], lon_bins)
                rf_y = np.digitize(point[2], lat_bins)
                if h < 24:
                    output_file.write('%d %f\n' % (point[0], diff2[h, rf_y, rf_x]))
                elif h < 48:
                    output_file.write('%d %f\n' % (point[0], diff1[h - 24, rf_y, rf_x]))
                elif h < 72:
                    output_file.write('%d %f\n' % (point[0], diff[h - 48, rf_y, rf_x] * alpha))
                else:
                    output_file.write('%d %f\n' % (point[0], diff[h - 48, rf_y, rf_x]))
        output_file.close()

    raincell_file_path = output_dir + '/RAINCELL.DAT'
    write_forecast_to_raincell_file(raincell_file_path, 1)

    for target_rf in [100, 150, 200, 250, 300]:
        write_forecast_to_raincell_file('%s.%d' % (raincell_file_path, target_rf), target_rf / avg_basin_rf)


def extract_kelani_upper_basin_mean_rainfall(nc_f, wrf_output, basin_shp_file=None, curw_db_adapter=None,
                                             curw_db_upsert=False, run_prefix='WRF', run_name='Cloud-1'):
    if basin_shp_file is None:
        basin_shp_file = res_mgr.get_resource_path('extraction/shp/kelani-upper-basin.shp')

    lon_min, lat_min, lon_max, lat_max = constants.KELANI_UPPER_BASIN_EXTENT

    nc_vars = ext_utils.extract_variables(nc_f, ['RAINC', 'RAINNC'], lat_min, lat_max, lon_min, lon_max)
    lats = nc_vars['XLAT']
    lons = nc_vars['XLONG']
    prcp = nc_vars['RAINC'] + nc_vars['RAINNC']
    times = nc_vars['Times']

    diff = prcp[1:len(times), :, :] - prcp[0:len(times) - 1, :, :]

    polys = shapefile.Reader(basin_shp_file)

    prefix = 'kub_mean_rf'
    output_dir = utils.create_dir_if_not_exists(os.path.join(wrf_output, prefix))

    with TemporaryDirectory(prefix=prefix) as temp_dir:
        output_file_path = os.path.join(temp_dir, prefix + '.txt')
        kub_rf = {}
        with open(output_file_path, 'w') as output_file:
            kub_rf['kub_mean'] = []
            for t in range(0, len(times) - 1):
                cnt = 0
                rf_sum = 0.0
                for y in range(0, len(lats)):
                    for x in range(0, len(lons)):
                        if utils.is_inside_polygon(polys, lats[y], lons[x]):
                            cnt = cnt + 1
                            rf_sum = rf_sum + diff[t, y, x]
                mean_rf = rf_sum / cnt

                t_str = (utils.datetime_utc_to_lk(dt.datetime.strptime(times[t], '%Y-%m-%d_%H:%M:%S'))).strftime(
                    '%Y-%m-%d %H:%M:%S')
                output_file.write('%s %f\n' % (t_str, mean_rf))
                kub_rf['kub_mean'].append([t_str, mean_rf])

        utils.move_files_with_prefix(temp_dir, '*.txt', output_dir)

    if curw_db_adapter is not None:
        name = 'kub_mean'
        station = [Station.CUrW, name, name, -999, -999, 0, 'Kelani upper basin mean rainfall']
        if ext_utils.create_station_if_not_exists(curw_db_adapter, station):
            logging.info('%s station created' % name)

        logging.info('Pushing data to the db...')
        ext_utils.push_rainfall_to_db(curw_db_adapter, kub_rf, upsert=curw_db_upsert, name=run_name,
                                      source=run_prefix)


def test_extract_kelani_upper_basin_mean_rainfall():
    # adapter = ext_utils.get_curw_adapter()
    adapter = None
    extract_kelani_upper_basin_mean_rainfall('/home/curw/Desktop/wrfout_d03_2017-07-31_00:00:00',
                                             dt.datetime.strptime('2017-07-31', '%Y-%m-%d'),
                                             '/home/curw/temp/', curw_db_adapter=adapter)


# def extract_kelani_upper_basin_mean_rainfall_sat(sat_dir, date, kelani_basin_shp_file, wrf_output):
#     kel_lon_min = 79.994117
#     kel_lat_min = 6.754167
#     kel_lon_max = 80.773182
#     kel_lat_max = 7.229167
#
#     y = date.strftime('%Y')
#     m = date.strftime('%m')
#     d = date.strftime('%d')
#
#     output_dir = wrf_output + '/kelani-upper-basin/sat'
#     if not os.path.exists(output_dir):
#         os.makedirs(output_dir)
#
#     output_file_path = output_dir + '/mean-rf-sat-' + date.strftime('%Y-%m-%d') + '.csv'
#     output_file = open(output_file_path, 'w')
#
#     polys = shapefile.Reader(kelani_basin_shp_file)
#
#     for h in range(0, 24):
#         cnt = 0
#         rf_sum = 0.0
#
#         sh = str(h).zfill(2)
#         sat_zip_file = '%s/%s/%s/%s/gsmap_nrt.%s%s%s.%s00.05_AsiaSS.csv.zip' % (sat_dir, y, m, d, y, m, d, sh)
#
#         sat_zip = zipfile.ZipFile(sat_zip_file)
#         sat = np.genfromtxt(sat_zip.open('gsmap_nrt.%s%s%s.%s00.05_AsiaSS.csv' % (y, m, d, sh)), delimiter=',',
#                             names=True)
#         sat_filt = sat[(sat['Lat'] <= kel_lat_max) & (sat['Lat'] >= kel_lat_min) & (sat['Lon'] <= kel_lon_max) & (
#             sat['Lon'] >= kel_lon_min)]
#
#         for p in sat_filt:
#             if utils.is_inside_polygon(polys, p[0], p[1]):
#                 cnt = cnt + 1
#                 rf_sum = rf_sum + p[2]
#
#         output_file.write('%s-%s-%s_%s:00:00 %f\n' % (y, m, d, sh, rf_sum / cnt))
#
#     output_file.close()
#
#
# def add_buffer_to_kelani_upper_basin_mean_rainfall(date, wrf_output):
#     cells = 9433
#
#     content = []
#     first_line = ''
#     for i in range(3, -1, -1):
#         file_name = wrf_output + '/kelani-basin/created-' + (date - dt.timedelta(days=i)).strftime(
#             '%Y-%m-%d') + '/RAINCELL.DAT'
#
#         if os.path.exists(file_name):
#             with open(file_name) as myfile:
#                 first_line = next(myfile)
#                 if i != 0:
#                     head = [next(myfile) for x in range(cells * 24)]
#                 else:
#                     head = [line for line in myfile]
#             content.extend(head)
#         else:
#             head = ['%d 0.0\n' % (x % cells + 1) for x in range(cells * 24 + 1)]
#             content.extend(head)
#
#     out_dir = wrf_output + '/kelani-basin/new-created-' + date.strftime('%Y-%m-%d')
#     out_name = out_dir + '/RAINCELL.DAT'
#
#     first_line = first_line.split()
#     first_line[1] = str(int(first_line[1]) + 24 * 3)
#     first_line[2] = (date - dt.timedelta(days=3)).strftime('%Y-%m-%d')
#
#     if not os.path.exists(out_dir):
#         os.makedirs(out_dir)
#     out_file = open(out_name, 'w')
#     out_file.write(' '.join(first_line) + '\n')
#     for line in content:
#         out_file.write(line)
#     out_file.close()
#
#
# def concat_rainfall_files(date, wrf_output, weather_stations):
#     with open(weather_stations, 'rb') as stations_file:
#         rf_dir = wrf_output + '/RF'
#         for station_name in stations_file:
#             station_name = station_name.split()[0]
#             if not os.path.exists(rf_dir):
#                 os.makedirs(rf_dir)
#
#             out_file_path = rf_dir + '/' + station_name + '.csv'
#             if not os.path.exists(out_file_path):
#                 with open(out_file_path, 'w') as out_file:
#                     out_file.write("Timestamp, Value, Time, ValID\n")
#
#             with open(out_file_path, 'a') as out_file:
#                 rf_file = rf_dir + '/' + station_name + '-' + date.strftime('%Y-%m-%d') + '.txt'
#                 with open(rf_file) as rf_file:
#                     # next(rf_file)
#                     i = 0
#                     for line in rf_file:
#                         ts = line.split()[0]
#                         val = line.split()[1]
#                         ref = int(dt.datetime.strptime('2017-04-01', '%Y-%m-%d').strftime('%s')) / 3600
#                         epoch = int(dt.datetime.strptime(ts, '%Y-%m-%d_%H:%M:%S').strftime('%s')) / 3600 - ref
#                         # epoch = dt.datetime.strptime(ts, '%Y-%m-%d_%H:%M:%S').strftime('%s')
#                         val_id = station_name[0:5] + date.strftime('%y%m%d-') + str(i / 24)
#                         out_file.write('%s, %s, %s, %s\n' % (ts, val, epoch, val_id))
#                         i += 1
#
#
# def concat_rainfall_files_1(date, wrf_output, weather_stations):
#     with open(weather_stations, 'rb') as stations_file:
#         rf_dir = wrf_output + '/RF'
#         for station_name in stations_file:
#             station_name = station_name.split()[0]
#             if not os.path.exists(rf_dir):
#                 os.makedirs(rf_dir)
#
#             df = None
#             out_file_path = rf_dir + '/' + station_name + '-merged.csv'
#             if os.path.exists(out_file_path):
#                 df = pd.read_csv(out_file_path)
#
#             rf_file = rf_dir + '/' + station_name + '-' + date.strftime('%Y-%m-%d') + '.txt'
#             rf_df = pd.read_csv(rf_file, header=None, delim_whitespace=True)
#             rf_df.columns = ['time', 'f' + date.strftime('%y%m%d')]
#             rf_df['time'] = rf_df['time'].apply(
#                 lambda x: int(dt.datetime.strptime(x, '%Y-%m-%d_%H:%M:%S').strftime('%s')))
#
#             if df is not None:
#                 df_out = pd.merge(df, rf_df, on='time', how='outer')
#                 df_out.to_csv(out_file_path, index=False)
#             else:
#                 rf_df.to_csv(out_file_path, index=False)


def extract_point_rf_series(nc_f, lat, lon):
    nc_fid = Dataset(nc_f, 'r')

    times_len, times = extract_time_data(nc_f)
    lats = nc_fid.variables['XLAT'][0, :, 0]
    lons = nc_fid.variables['XLONG'][0, 0, :]

    lat_start_idx = np.argmin(abs(lats - lat))
    lon_start_idx = np.argmin(abs(lons - lon))

    prcp = nc_fid.variables['RAINC'][:, lat_start_idx, lon_start_idx] + \
           nc_fid.variables['RAINNC'][:, lat_start_idx, lon_start_idx] + \
           nc_fid.variables['SNOWNC'][:, lat_start_idx, lon_start_idx] + \
           nc_fid.variables['GRAUPELNC'][:, lat_start_idx, lon_start_idx]

    diff = prcp[1:times_len] - prcp[0:times_len - 1]

    nc_fid.close()

    return diff, np.array(times[0:times_len - 1])


def extract_area_rf_series(nc_f, lat_min, lat_max, lon_min, lon_max):
    if not os.path.exists(nc_f):
        raise IOError('File %s not found' % nc_f)

    nc_fid = Dataset(nc_f, 'r')

    times_len, times = extract_time_data(nc_f)
    lats = nc_fid.variables['XLAT'][0, :, 0]
    lons = nc_fid.variables['XLONG'][0, 0, :]

    lon_min_idx = np.argmax(lons >= lon_min) - 1
    lat_min_idx = np.argmax(lats >= lat_min) - 1
    lon_max_idx = np.argmax(lons >= lon_max)
    lat_max_idx = np.argmax(lats >= lat_max)

    prcp = nc_fid.variables['RAINC'][:, lat_min_idx:lat_max_idx, lon_min_idx:lon_max_idx] + \
           nc_fid.variables['RAINNC'][:, lat_min_idx:lat_max_idx, lon_min_idx:lon_max_idx] + \
           nc_fid.variables['SNOWNC'][:, lat_min_idx:lat_max_idx, lon_min_idx:lon_max_idx] + \
           nc_fid.variables['GRAUPELNC'][:, lat_min_idx:lat_max_idx, lon_min_idx:lon_max_idx]

    diff = prcp[1:times_len] - prcp[0:times_len - 1]

    nc_fid.close()

    return diff, lats[lat_min_idx:lat_max_idx], lons[lon_min_idx:lon_max_idx], np.array(times[0:times_len - 1])


def extract_jaxa_weather_stations(nc_f, weather_stations_file, output_dir):
    nc_fid = Dataset(nc_f, 'r')

    stations = pd.read_csv(weather_stations_file, header=0, sep=',')

    output_file_dir = os.path.join(output_dir, 'jaxa-stations-wrf-forecast')
    utils.create_dir_if_not_exists(output_file_dir)

    for idx, station in stations.iterrows():
        logging.info('Extracting station ' + str(station))

        rf, times = extract_point_rf_series(nc_f, station[2], station[1])

        output_file_path = os.path.join(output_file_dir,
                                        station[3] + '-' + str(station[0]) + '-' + times[0].split('_')[0] + '.txt')
        output_file = open(output_file_path, 'w')
        output_file.write('jaxa-stations-wrf-forecast\n')
        output_file.write(', '.join(stations.columns.values) + '\n')
        output_file.write(', '.join(str(x) for x in station) + '\n')
        output_file.write('timestamp, rainfall\n')
        for i in range(len(times)):
            output_file.write('%s, %f\n' % (times[i], rf[i]))
        output_file.close()

    nc_fid.close()


def extract_jaxa_satellite_data(start_ts_utc, end_ts_utc, output_dir):
    start = utils.datetime_floor(start_ts_utc, 3600)
    end = utils.datetime_floor(end_ts_utc, 3600)

    lat_min = 5.722969
    lon_min = 79.52146
    lat_max = 10.06425
    lon_max = 82.18992

    login = 'rainmap:Niskur+1404'

    url0 = 'ftp://' + login + '@hokusai.eorc.jaxa.jp/realtime/txt/05_AsiaSS/YYYY/MM/DD/gsmap_nrt.YYYYMMDD.HH00.05_AsiaSS.csv.zip'
    url1 = 'ftp://' + login + '@hokusai.eorc.jaxa.jp/now/txt/05_AsiaSS/gsmap_now.YYYYMMDD.HH00_HH59.05_AsiaSS.csv.zip'

    def get_jaxa_url(ts):
        url_switch = (dt.datetime.utcnow() - ts) > dt.timedelta(hours=5)
        _url = url0 if url_switch else url1
        ph = {'YYYY': ts.strftime('%Y'),
              'MM': ts.strftime('%m'),
              'DD': ts.strftime('%d'),
              'HH': ts.strftime('%H')}
        for k, v in ph.iteritems():
            _url = _url.replace(k, v)
        return _url

    tmp_dir = os.path.join(output_dir, 'tmp_jaxa/')
    if not os.path.exists(tmp_dir):
        os.mkdir(tmp_dir)
    else:
        utils.cleanup_dir(tmp_dir)

    url_dest_list = []
    for timestamp in np.arange(start, end, dt.timedelta(hours=1)).astype(dt.datetime):
        url = get_jaxa_url(timestamp)
        url_dest_list.append((url, os.path.join(tmp_dir, os.path.basename(url)),
                              os.path.join(output_dir, 'jaxa_sat_rf_' + timestamp.strftime('%Y-%m-%d_%H:%M') + '.asc')))

    utils.download_parallel(url_dest_list)

    procs = multiprocessing.cpu_count()
    Parallel(n_jobs=procs)(
        delayed(_process_zip_file)(i[1], i[2], lat_min, lon_min, lat_max, lon_max) for i in url_dest_list)

    # clean up temp dir
    shutil.rmtree(tmp_dir)


def _process_zip_file(zip_file_path, out_file_path, lat_min, lon_min, lat_max, lon_max):
    sat_zip = zipfile.ZipFile(zip_file_path)
    sat = np.genfromtxt(sat_zip.open(os.path.basename(zip_file_path).replace('.zip', '')), delimiter=',', names=True)
    sat_filt = sat[
        (sat['Lat'] <= lat_max) & (sat['Lat'] >= lat_min) & (sat['Lon'] <= lon_max) & (sat['Lon'] >= lon_min)]
    lats = np.sort(np.unique(sat_filt['Lat']))
    lons = np.sort(np.unique(sat_filt['Lon']))

    cell_size = 0.1
    no_data_val = -99
    out_file = open(out_file_path, 'w')
    out_file.write('NCOLS %d\n' % len(lons))
    out_file.write('NROWS %d\n' % len(lats))
    out_file.write('XLLCORNER %f\n' % lons[0])
    out_file.write('YLLCORNER %f\n' % lats[0])
    out_file.write('CELLSIZE %f\n' % cell_size)
    out_file.write('NODATA_VALUE %d\n' % no_data_val)

    for lat in np.flip(lats, 0):
        for lon in lons:
            out_file.write(str(sat[(sat['Lat'] == lat) & (sat['Lon'] == lon)][0][2]) + ' ')
        out_file.write('\n')

    out_file.close()


# def extract_all(wrf_home, start_date, end_date):
#     logging.info('Extracting data from %s to %s' % (start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')))
#     logging.info('WRF home : %s' % wrf_home)
#
#     weather_st_file = res_mgr.get_resource_path('extraction/local/kelani_basin_stations.txt')
#     # kelani_basin_file = res_mgr.get_resource_path('extraction/local/kelani_basin_points.txt')
#     kelani_basin_file = res_mgr.get_resource_path('extraction/local/kelani_basin_points_250m.txt')
#     kelani_basin_shp_file = res_mgr.get_resource_path('extraction/shp/kelani-upper-basin.shp')
#     jaxa_weather_st_file = res_mgr.get_resource_path('extraction/local/jaxa_weather_stations.txt')
#
#     dates = np.arange(start_date, end_date, dt.timedelta(days=1)).astype(dt.datetime)
#
#     for date in dates:
#         wrf_output = utils.get_output_dir(wrf_home)
#
#         nc_f = wrf_output + '/wrfout_d03_' + date.strftime('%Y-%m-%d') + '_00:00:00'
#         if not os.path.exists(nc_f):
#             raise IOError('File %s not found' % nc_f)
#
#         logging.info('Extracting time data')
#         times_len, times = extract_time_data(nc_f)
#
#         logging.info('Extract rainfall data for the metro colombo area')
#         basin_rf = extract_metro_colombo(nc_f, date, wrf_output)
#         logging.info('Basin rainfall' + str(basin_rf))
#
#         logging.info('Extract weather station rainfall')
#         extract_weather_stations(nc_f, date, wrf_output, weather_st_file)
#
#         logging.info('Extract Kelani Basin rainfall')
#         extract_kelani_basin_rainfall(nc_f, date, wrf_output, avg_basin_rf=basin_rf,
#                                       kelani_basin_file=kelani_basin_file)
#
#         logging.info('Extract Kelani upper Basin mean rainfall')
#         extract_kelani_upper_basin_mean_rainfall(nc_f, date, times, kelani_basin_shp_file, wrf_output)
#
#         logging.info('Extract Jaxa stations wrf rainfall')
#         # extract_jaxa_weather_stations(nc_f, jaxa_weather_st_file, wrf_output)
#
#         logging.info('Exctract Jaxa sattellite rainfall data')
#         # extract_jaxa_satellite_data(date, wrf_output)
#
#         # logging.info('adding buffer to the RAINCELL.DAT file')
#         # add_buffer_to_kelani_upper_basin_mean_rainfall(date, wrf_output)
#
#         # logging.info('Concat the RF of the weather stations 1')
#         # concat_rainfall_files(date, wrf_output, weather_st_file)
#
#         # logging.info('Concat the RF of the weather stations 2')
#         # concat_rainfall_files_1(date, wrf_output, weather_st_file)
#
#         # print "##########################"
#         # print "Analyze the Sat Images"
#         # sat_data_dir = '/home/nira/Desktop/2016-event/05_AsiaSS'
#         # extract_kelani_upper_basin_mean_rainfall_sat(sat_data_dir, date, kelani_basin_shp_file, wrf_output)
#
#
# if __name__ == "__main__":
#     # extract_jaxa_satellite_data(utils.datetime_lk_to_utc(dt.datetime(2017, 5, 25)),
#     #                             utils.datetime_lk_to_utc(dt.datetime(2017, 5, 28)), '/tmp/rf')
#     logging.basicConfig(level=logging.INFO, format='%(asctime)s %(threadName)s %(module)s %(levelname)s %(message)s')
#     args = utils.parse_args()
#
#     wh = args.wrf_home
#     sd = dt.datetime.strptime(args.start_date, '%Y-%m-%d')
#     ed = dt.datetime.strptime(args.end_date, '%Y-%m-%d')
#     p = args.period
#
#     extract_all(wh, sd, ed)


def push_wrf_rainfall_to_db(nc_f, curw_db_adapter=None, lon_min=None, lat_min=None, lon_max=None,
                            lat_max=None, run_prefix='WRF', upsert=False, run_name='Cloud-1'):
    """

    :param run_name: 
    :param nc_f:
    :param curw_db_adapter: If not none, data will be pushed to the db
    :param run_prefix:
    :param lon_min:
    :param lat_min:
    :param lon_max:
    :param lat_max:
    :param upsert: 
    :return:
    """
    if not all([lon_min, lat_min, lon_max, lat_max]):
        lon_min, lat_min, lon_max, lat_max = constants.SRI_LANKA_EXTENT

    nc_vars = ext_utils.extract_variables(nc_f, ['RAINC', 'RAINNC'], lat_min, lat_max, lon_min, lon_max)
    lats = nc_vars['XLAT']
    lons = nc_vars['XLONG']
    prcp = nc_vars['RAINC'] + nc_vars['RAINNC']
    times = nc_vars['Times']

    diff = prcp[1:len(times), :, :] - prcp[0:len(times) - 1, :, :]

    width = len(lons)
    height = len(lats)

    def random_check_stations_exist():
        for _ in range(10):
            _x = lons[int(random() * width)]
            _y = lats[int(random() * height)]
            _name = '%s_%.6f_%.6f' % (run_prefix, _x, _y)
            _query = {'name': _name}
            if curw_db_adapter.get_station(_query) is None:
                logging.debug('Random stations check fail')
                return False
        logging.debug('Random stations check success')
        return True

    stations_exists = random_check_stations_exist()

    rf_ts = {}
    for y in range(height):
        for x in range(width):
            lat = lats[y]
            lon = lons[x]

            station_id = '%s_%.6f_%.6f' % (run_prefix, lon, lat)
            name = station_id

            if not stations_exists:
                logging.info('Creating station %s ...' % name)
                station = [Station.WRF, station_id, name, str(lon), str(lat), str(0), "WRF point"]
                curw_db_adapter.create_station(station)

            # add rf series to the dict
            ts = []
            for i in range(len(diff)):
                t = utils.datetime_utc_to_lk(dt.datetime.strptime(times[i], '%Y-%m-%d_%H:%M:%S'))
                ts.append([t.strftime('%Y-%m-%d %H:%M:%S'), diff[i, y, x]])
            rf_ts[name] = ts

    ext_utils.push_rainfall_to_db(curw_db_adapter, rf_ts, source=run_prefix, upsert=upsert, name=run_name)


def create_rf_plots_wrf(nc_f, wrf_output, wrf_output_base, lon_min=None, lat_min=None, lon_max=None, lat_max=None,
                        filter_threshold=0.05, run_prefix='WRF'):
    if not all([lon_min, lat_min, lon_max, lat_max]):
        lon_min, lat_min, lon_max, lat_max = constants.SRI_LANKA_EXTENT

    d03_dir = os.path.join(wrf_output, 'plots_d03')

    variables = ext_utils.extract_variables(nc_f, 'RAINC, RAINNC', lat_min, lat_max, lon_min, lon_max)

    lats = variables['XLAT']
    lons = variables['XLONG']

    # cell size is calc based on the mean between the lat and lon points
    cz = np.round(np.mean(np.append(lons[1:len(lons)] - lons[0: len(lons) - 1], lats[1:len(lats)]
                                    - lats[0: len(lats) - 1])), 3)
    clevs = [0, 1, 2.5, 5, 7.5, 10, 15, 20, 30, 40, 50, 70, 100, 150, 200, 250, 300, 400, 500, 600, 750]
    cmap = cm.s3pcpn

    basemap = Basemap(projection='merc', llcrnrlon=lon_min, llcrnrlat=lat_min, urcrnrlon=lon_max,
                      urcrnrlat=lat_max, resolution='h')

    data = variables['RAINC'] + variables['RAINNC']
    logging.info('Filtering with the threshold %f' % filter_threshold)
    data[data < filter_threshold] = 0.0
    variables['PRECIP'] = data

    prefix = 'wrf_plots'
    with TemporaryDirectory(prefix=prefix) as temp_dir:
        for i in range(1, len(variables['Times'])):
            time = variables['Times'][i]
            ts = dt.datetime.strptime(time, '%Y-%m-%d_%H:%M:%S')
            lk_ts = utils.datetime_utc_to_lk(ts)
            logging.info('processing %s', time)

            # instantaneous precipitation (hourly)
            inst_precip = variables['PRECIP'][i] - variables['PRECIP'][i - 1]

            inst_file = os.path.join(temp_dir, 'wrf_inst_' + lk_ts.strftime('%Y-%m-%d_%H:%M:%S'))

            ext_utils.create_asc_file(np.flip(inst_precip, 0), lats, lons, inst_file + '.asc', cell_size=cz)

            title = {
                'label': 'Hourly rf for %s LK' % lk_ts.strftime('%Y-%m-%d_%H:%M:%S'),
                'fontsize': 30
            }
            ext_utils.create_contour_plot(inst_precip, inst_file + '.png', lat_min, lon_min, lat_max, lon_max,
                                          title, clevs=clevs, cmap=cmap, basemap=basemap)

            if i % 24 == 0:
                t = 'Daily rf from %s LK to %s LK' % (
                    (lk_ts - dt.timedelta(hours=24)).strftime('%Y-%m-%d_%H:%M:%S'), lk_ts.strftime('%Y-%m-%d_%H:%M:%S'))
                d = int(i / 24) - 1
                logging.info('Creating images for D%d' % d)
                cum_file = os.path.join(temp_dir, 'wrf_cum_%dd' % d)

                ext_utils.create_asc_file(np.flip(variables['PRECIP'][i], 0), lats, lons, cum_file + '.asc',
                                          cell_size=cz)

                ext_utils.create_contour_plot(variables['PRECIP'][i] - variables['PRECIP'][i - 24], cum_file + '.png',
                                              lat_min, lon_min, lat_max, lon_max, t, clevs=clevs, cmap=cmap,
                                              basemap=basemap)

                gif_file = os.path.join(temp_dir, 'wrf_inst_%dd' % d)
                images = [os.path.join(temp_dir, 'wrf_inst_' + i.strftime('%Y-%m-%d_%H:%M:%S') + '.png') for i in
                          np.arange(lk_ts - dt.timedelta(hours=23), lk_ts + dt.timedelta(hours=1),
                                    dt.timedelta(hours=1)).astype(dt.datetime)]
                ext_utils.create_gif(images, gif_file + '.gif')

        logging.info('Creating the zips')
        utils.create_zip_with_prefix(temp_dir, '*.png', os.path.join(temp_dir, 'pngs.zip'))
        utils.create_zip_with_prefix(temp_dir, '*.asc', os.path.join(temp_dir, 'ascs.zip'))

        logging.info('Cleaning up instantaneous pngs and ascs - wrf_inst_*')
        utils.delete_files_with_prefix(temp_dir, 'wrf_inst_*.png')
        utils.delete_files_with_prefix(temp_dir, 'wrf_inst_*.asc')

        logging.info('Copying pngs to ' + d03_dir)
        utils.move_files_with_prefix(temp_dir, '*.png', d03_dir)
        logging.info('Copying ascs to ' + d03_dir)
        utils.move_files_with_prefix(temp_dir, '*.asc', d03_dir)
        logging.info('Copying gifs to ' + d03_dir)
        utils.copy_files_with_prefix(temp_dir, '*.gif', d03_dir)
        logging.info('Copying zips to ' + d03_dir)
        utils.copy_files_with_prefix(temp_dir, '*.zip', d03_dir)

        d03_latest_dir = os.path.join(wrf_output_base, 'latest', run_prefix)
        # <nfs>/latest/wrf0 .. 3
        utils.create_dir_if_not_exists(d03_latest_dir)
        # todo: this needs to be adjusted to handle the multiple runs
        logging.info('Copying gifs to ' + d03_latest_dir)
        utils.copy_files_with_prefix(temp_dir, '*.gif', d03_latest_dir)


def suite():
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(threadName)s %(module)s %(levelname)s %(message)s')
    s = unittest.TestSuite()
    s.addTest(TestExtractor)
    return s


class TestExtractor(unittest.TestCase):
    def test_push_wrf_rainfall_to_db(self):
        config = {
            "host": "localhost",
            "user": "test",
            "password": "password",
            "db": "testdb"
        }
        adapter = ext_utils.get_curw_adapter(mysql_config=config)

        nc_f = res_mgr.get_resource_path('test/wrfout_d03_2017-10-02_12:00:00')
        lon_min, lat_min, lon_max, lat_max = constants.KELANI_KALU_BASIN_EXTENT
        push_wrf_rainfall_to_db(nc_f, curw_db_adapter=adapter, lat_min=lat_min, lon_min=lon_min,
                                lat_max=lat_max, lon_max=lon_max, upsert=True)
