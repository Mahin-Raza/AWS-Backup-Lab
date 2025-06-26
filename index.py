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

def handler(event, context):
    final_result={
        'totalScore' : 0,
        'splitScore' : { 'Check1' : 0,'Check2' : 0}
    }
    #Check1=CreateTag
    #Check2=DescribeTag
    Backup_Role = None
    Backup_Vault = None
    #print(event)
    outputs = event['outputs']
    for e in outputs:
        if e['OutputKey'] == 'BackupRole':
            Backup_Role = e['OutputValue']
        if e['OutputKey'] == 'BackupVault':
            Backup_Vault = e['OutputValue']
            
    #print(Backup_Role)
    #print(Backup_Vault)
    AWS_REGION = 'eu-west-2'
    iam = boto3.client('iam')
    backup = boto3.client('backup')
    
    response = backup.list_recovery_points_by_backup_vault(
    BackupVaultName=Backup_Vault)
    
    #check if there are any backups in the given vault, if there are non then return the score zero.
    if len(response) < 1  :
        print("No successful backups found for the given EBS in the vault")
        delete_recovery_points(Backup_Vault)
        return final_result

    result = iam.get_role_policy(RoleName=Backup_Role, PolicyName='backuppolicy')
    #print(result)
    policy = json.dumps(result['PolicyDocument'])
    #print(policy)

    if "ec2:CreateTags" in policy:
            #print("CreateTags there")
            final_result['splitScore']['Check1'] = 50
    else:
            final_result['splitScore']['Check1'] = 0
    
    if "ec2:DescribeTags" in policy:
            #print("DescribeTags there")
            final_result['splitScore']['Check2'] = 50
    else :
            final_result['splitScore']['Check2'] = 0          
    
    final_result['totalScore'] = final_result['splitScore']['Check1']+final_result['splitScore']['Check2']
    delete_recovery_points(Backup_Vault)
    return final_result

def delete_recovery_points(vault_name: Backup_Vault):
    backup = boto3.client('backup')
    response = backup.list_recovery_points_by_backup_vault(
    BackupVaultName=Backup_Vault)
    
    while 'NextToken' in response:
    for recovery_point in response['RecoveryPoints']:
        backup.delete_recovery_point(
            BackupVaultName=vault_name,
            RecoveryPointArn=recovery_point['RecoveryPointArn']
        )

    response = backup.list_recovery_points_by_backup_vault(
        BackupVaultName=vault_name,
        NextToken=response['NextToken']
    )

    for recovery_point in response['RecoveryPoints']:
        backup.delete_recovery_point(
            BackupVaultName=vault_name,
            RecoveryPointArn=recovery_point['RecoveryPointArn']
        )
