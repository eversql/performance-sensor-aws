from __future__ import print_function

import argparse
import os.path
import sys
import boto3
import json
import urllib3
import base64
import time
import urllib.parse
import datetime
import zlib
from urllib import request, parse
from botocore.exceptions import NoRegionError, ClientError

def lambda_handler(event, context):
    start_time = time.time()
    
    api_key = os.getenv('EVERSQL_API_KEY')
    region = os.getenv('AWS_REGION_TO_TRACK')
    databases_to_track = os.getenv('AWS_DB_SERVERS_LIST_TO_TRACK')
    
    rds_api = boto3.client('rds', region)
    lambda_api = boto3.client('lambda')
    
    rds_instance_list = databases_to_track.split(",")
    if (len(rds_instance_list) == 1 and rds_instance_list[0] == ''):
        print('The value of AWS_DB_SERVERS_LIST_TO_TRACK is invalid. It should contain a comma delimited list of database names. The current value is: ' + databases_to_track)
        return
    
    print('Databases to track: ' + ','.join(rds_instance_list))    
    
    for rds_instance in rds_instance_list:
        try:
            # If we passed the 5 minutes runtime, exit
            if ((time.time() - start_time) > 300):
                return
            
            print('Started fetching data from the database: ' + rds_instance)
            
            # Getting a list of all log files which were updated since the last checkpoint
            # Also, getting the last file we parsed, and its marker, to make sure we continue from
            # the same place we stopped the last time.
            function_curr_tags = lambda_api.list_tags(Resource=context.invoked_function_arn)
            timestamp_to_start_with = 1577836800
            marker = '0'
            last_parsed_file = ''
            if (rds_instance in function_curr_tags['Tags']):
                last_timestamp_and_marker_parsed = function_curr_tags['Tags'][rds_instance]
                last_timestamp_and_marker_parsed_split = last_timestamp_and_marker_parsed.split("_")
                last_parsed_file = last_timestamp_and_marker_parsed_split[0]
                timestamp_to_start_with = (int)(last_timestamp_and_marker_parsed_split[1])
                marker = last_timestamp_and_marker_parsed_split[2]
                print('Last parsed file: ' + last_parsed_file + ', time: ' + (str)(timestamp_to_start_with) + ', marker: ' + marker)
            
            log_files_list = rds_api.describe_db_log_files(DBInstanceIdentifier=rds_instance, FilenameContains='slow', FileSize=1024, FileLastWritten=timestamp_to_start_with)
            log_files_list['DescribeDBLogFiles'].sort(key=lambda r: r['LastWritten'], reverse=False)
    
            if (len(log_files_list['DescribeDBLogFiles']) > 0):
                db_log = log_files_list['DescribeDBLogFiles'][0]
                log_file = db_log['LogFileName']
                print('Fetching queries from file: ' + log_file)
                
                # If the current file is different than the previous one, reseting the marker
                # Also, we need to make sure that if we just rolled over to the next file, we still look
                # at the file from the last marker (slowquery/mysql-slowquery.log => slowquery/mysql-slowquery.X.log)
                if ((last_parsed_file != log_file) and not (last_parsed_file == 'slowquery/mysql-slowquery.log' and len(log_files_list) > 1)):
                    marker = '0'
                
                # Reading the first portion (up to 9999 rows or 1MB)
                log_file_portion = rds_api.download_db_log_file_portion(DBInstanceIdentifier=rds_instance, LogFileName=log_file, Marker=marker, NumberOfLines=1)
                marker = log_file_portion['Marker']
                last_marker = '0'
                log_data = log_file_portion['LogFileData']
        
                # Continue reading until we have data or if we reached the max chunk size
                while last_marker != marker and (log_file_portion == None or len(log_file_portion['LogFileData']) > 0):
                    try:
                        log_file_portion = rds_api.download_db_log_file_portion(DBInstanceIdentifier=rds_instance, LogFileName=log_file, Marker=marker, NumberOfLines=500)
                        last_marker = marker
                        marker = log_file_portion['Marker']
                        log_data += log_file_portion['LogFileData'];
                        
                        if (len(log_data) >= 3145728):
                            submit_logs(rds_instance, log_data, api_key)
                            log_data = ''
                    except ClientError as e:
                        log_file_portion = None
                        marker_parts = marker.split(":")
                        skipped_marker = ((int)(marker_parts[1])) + 500
                        marker = ((str)(marker_parts[0])) + ":" + ((str)(skipped_marker))
    
                if (len(log_data) > 0):
                    submit_logs(rds_instance, log_data, api_key)
                    
                # Save the last timestamp to start from in the next round
                lambda_api.tag_resource(Resource=context.invoked_function_arn, Tags={rds_instance : log_file + '_' + (str)((int)(db_log['LastWritten']) + 1000) + "_" + (str)(marker)})
        except Exception as e:
            print('Failed fetching logs from: ' + rds_instance)
            print(e)
            sys.exit(2)
        
# Submit the queries to the API
def submit_logs(server_name, log_data, apikey):
    print('Submitting ' + (str)(len(log_data)) + ' bytes')
    data = base64.b64encode(zlib.compress(log_data.encode("utf-8")))
    headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2228.0 Safari/537.3'}
    req =  request.Request(url='https://actions.eversql.com/upload?compressed=true&server=' + server_name + '&apikey=' + apikey, data=data, headers=headers)
    resp = request.urlopen(req)