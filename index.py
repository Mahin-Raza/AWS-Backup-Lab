from __future__ import print_function # Python 2/3 compatibility
import json
import base64
import boto3
import time
import os
import boto3
import botocore
import logging
from botocore.exceptions import ClientError
from botocore.vendored import requests
import json
import boto3
from datetime import datetime, timedelta
import logging

def validate_tags(cluster_arn):
    """Validate tags on Aurora cluster"""
    try:
        rds_client = boto3.client('rds')
        
        # Get cluster tags
        response = rds_client.list_tags_for_resource(
            ResourceName=cluster_arn
        )
        
        required_tags = {'backup-policy': 'daily'}
        cluster_tags = {tag['Key']: tag['Value'] for tag in response['TagList']}
        
        # Check if required tags exist with correct values
        for key, value in required_tags.items():
            if key not in cluster_tags or cluster_tags[key] != value:
                return False, f"Missing or incorrect tag: {key}={value}"
        
        return True, "All required tags found"
        
    except Exception as e:
        return False, f"Error validating tags: {str(e)}"


def validate_backup_selection(backup_plan_name, cluster_arn):
    """Validate backup selection configuration"""
    try:
        backup_client = boto3.client('backup')
        
        # Get Backup Plan
        backup_plans = backup_client.list_backup_plans()
        target_plan = None
        for plan in backup_plans['BackupPlansList']:
            if plan['BackupPlanName'] == backup_plan_name:
                selections = backup_client.list_backup_selections(
                    BackupPlanId=plan['BackupPlanId']
                )
                
                # Check if cluster_arn is in selections
                for selection in selections['BackupSelectionsList']:
                    selection_details = backup_client.get_backup_selection(
                        BackupPlanId=plan['BackupPlanId'],
                        SelectionId=selection['SelectionId']
                    )
                    if cluster_arn in selection_details['BackupSelection']['Resources']:
                        return True, "Cluster ARN found in backup selection"
                
                return False, "Cluster ARN not found in backup selection"
        
        return False, f"Backup plan {backup_plan_name} not found"
        
    except Exception as e:
        return False, f"Error validating backup selection: {str(e)}"


def parse_cron_window(cron_expression, window_minutes):
    """
    Parse AWS Backup cron expression without croniter
    Format expected: 'cron(0 5 ? * * *)'
    Returns start and end time
    """
    try:
        # Remove cron() wrapper and split
        cron_parts = cron_expression.replace('cron(', '').replace(')', '').split()
        
        # Get minute and hour from cron expression
        minute = int(cron_parts[0]) if cron_parts[0] != '*' else 0
        hour = int(cron_parts[1]) if cron_parts[1] != '*' else 0
        
        # Create time objects
        now = datetime.now()
        start_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        end_time = start_time + timedelta(minutes=window_minutes)
        
        return start_time, end_time
    except Exception as e:
        raise ValueError(f"Invalid cron expression format: {str(e)}")

def check_backup_windows(backup_plan_name, cluster_arn):
    """
    Check backup windows for:
    1. No overlap between backup rules
    2. No overlap with 1:00-3:00 UTC
    """
    try:
        backup_client = boto3.client('backup')
        
        # Define the fixed UTC window (1:00-3:00 UTC)
        now = datetime.now()
        utc_start = now.replace(hour=1, minute=0)
        utc_end = now.replace(hour=3, minute=0)
        
        # Get Backup Plan
        backup_plans = backup_client.list_backup_plans()
        target_plan = None
        for plan in backup_plans['BackupPlansList']:
            if plan['BackupPlanName'] == backup_plan_name:
                target_plan = backup_client.get_backup_plan(
                    BackupPlanId=plan['BackupPlanId']
                )
                break
        
        if not target_plan:
            return False, f"Backup plan {backup_plan_name} not found"

        # Get backup rules windows
        backup_windows = []
        for rule in target_plan['BackupPlan']['Rules']:
            try:
                start_time, end_time = parse_cron_window(
                    rule['ScheduleExpression'], 
                    rule['CompletionWindowMinutes']
                )
                
                backup_windows.append({
                    'start': start_time,
                    'end': end_time,
                    'name': rule['RuleName']
                })
            except Exception as e:
                return False, f"Error parsing schedule for rule {rule['RuleName']}: {str(e)}"

        # Check for overlaps
        issues = {
            'rule_overlaps': [],
            'utc_window_overlaps': []
        }

        # Check overlaps between backup rules
        for i, window1 in enumerate(backup_windows):
            for j, window2 in enumerate(backup_windows[i+1:], i+1):
                # Convert times to comparable format (minutes since midnight)
                w1_start = window1['start'].hour * 60 + window1['start'].minute
                w1_end = window1['end'].hour * 60 + window1['end'].minute
                w2_start = window2['start'].hour * 60 + window2['start'].minute
                w2_end = window2['end'].hour * 60 + window2['end'].minute
                
                if (w1_start <= w2_end and w2_start <= w1_end):
                    overlap_start_mins = max(w1_start, w2_start)
                    overlap_end_mins = min(w1_end, w2_end)
                    overlap_minutes = overlap_end_mins - overlap_start_mins
                    
                    # Convert back to HH:MM format
                    overlap_start = f"{overlap_start_mins//60:02d}:{overlap_start_mins%60:02d}"
                    overlap_end = f"{overlap_end_mins//60:02d}:{overlap_end_mins%60:02d}"
                    
                    issues['rule_overlaps'].append({
                        "Rule1": window1['name'],
                        "Rule2": window2['name'],
                        "OverlapMinutes": overlap_minutes,
                        "OverlapPeriod": f"{overlap_start} - {overlap_end}"
                    })

        # Check overlaps with UTC window (1:00-3:00)
        utc_start_mins = 60  # 1:00 UTC
        utc_end_mins = 180   # 3:00 UTC
        
        for window in backup_windows:
            window_start_mins = window['start'].hour * 60 + window['start'].minute
            window_end_mins = window['end'].hour * 60 + window['end'].minute
            
            if (window_start_mins <= utc_end_mins and utc_start_mins <= window_end_mins):
                overlap_start_mins = max(window_start_mins, utc_start_mins)
                overlap_end_mins = min(window_end_mins, utc_end_mins)
                overlap_minutes = overlap_end_mins - overlap_start_mins
                
                # Convert to HH:MM format
                overlap_start = f"{overlap_start_mins//60:02d}:{overlap_start_mins%60:02d}"
                overlap_end = f"{overlap_end_mins//60:02d}:{overlap_end_mins%60:02d}"
                
                issues['utc_window_overlaps'].append({
                    "RuleName": window['name'],
                    "OverlapMinutes": overlap_minutes,
                    "OverlapPeriod": f"{overlap_start} - {overlap_end}"
                })

        # Prepare response
        if issues['rule_overlaps'] or issues['utc_window_overlaps']:
            error_messages = []
            
            if issues['rule_overlaps']:
                error_messages.append("Backup Rule Overlaps:")
                for overlap in issues['rule_overlaps']:
                    error_messages.append(
                        f"- Overlap between {overlap['Rule1']} and {overlap['Rule2']}\n"
                        f"  Period: {overlap['OverlapPeriod']}\n"
                        f"  Duration: {overlap['OverlapMinutes']} minutes"
                    )
            
            if issues['utc_window_overlaps']:
                if issues['rule_overlaps']:
                    error_messages.append("\n")
                error_messages.append("Overlaps with 1:00-3:00 UTC window:")
                for overlap in issues['utc_window_overlaps']:
                    error_messages.append(
                        f"- Rule: {overlap['RuleName']}\n"
                        f"  Period: {overlap['OverlapPeriod']}\n"
                        f"  Duration: {overlap['OverlapMinutes']} minutes"
                    )
            
            return False, "\n".join(error_messages)
        
        # Create window summary for successful case
        window_summary = ["Backup Windows Configuration:"]
        window_summary.append("- Reserved UTC window: 01:00 - 03:00")
        window_summary.append("\nConfigured Backup Rules:")
        for window in sorted(backup_windows, key=lambda x: x['start']):
            window_start = f"{window['start'].hour:02d}:{window['start'].minute:02d}"
            window_end = f"{window['end'].hour:02d}:{window['end'].minute:02d}"
            window_summary.append(
                f"- {window['name']}: {window_start} - {window_end}"
            )
        
        return True, "\n".join(window_summary)
        
    except Exception as e:
        return False, f"Error checking backup windows: {str(e)}"


def handler(event, context):
    #Check1=BackupSelectionTagAndClusterArn
    #Check2=BackupWindowConflict
        final_result={
        'totalScore' : 0,
        'splitScore' : { 'Check1' : 0,'Check2' : 0}
        }

        outputs = event.get('outputs', {})
        if 'outputs' not in event:
            raise ValueError("Missing 'Outputs' in event data")

        outputs = event['outputs']
        for e in outputs:
            if e['OutputKey'] == 'BackupPlanName':
                backup_plan_name = e['OutputValue']
            if e['OutputKey'] == 'ClusterArn':
                cluster_arn = e['OutputValue']
        #print(event)
        #print(backup_plan_name)
        #print(cluster_arn)
        

        
        # Validate backup windows
        windows_valid, windows_message = check_backup_windows(backup_plan_name, cluster_arn)
        #print(windows_message)
        
        # Validate backup selection
        selection_valid, selection_message = validate_backup_selection(backup_plan_name, cluster_arn)
        #print(selection_message)

        # Validate tags
        tags_valid, tags_message = validate_tags(cluster_arn)
        #print(tags_message)
        
        # Compile results
        
        if windows_valid :
            final_result['splitScore']['Check1']=50
        else:
            final_result['splitScore']['Check1']=0
                   
            
        if selection_valid and tags_valid:
            final_result['splitScore']['Check2']=50
        else:
            final_result['splitScore']['Check2']=0
 
        # Overall validation status
        final_result['totalScore'] = final_result['splitScore']['Check1']+final_result['splitScore']['Check2']
        #print(final_result)
        return final_result
    