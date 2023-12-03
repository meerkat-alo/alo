# from ruamel.yaml import YAML
import sys
import boto3
import os
import re
import git
import shutil
import datetime
import yaml 
from yaml import Dumper
import botocore
from botocore.exceptions import ClientError, NoCredentialsError
import subprocess
# 모듈 import 
import os
import json
import requests
# yaml = YAML()
# yaml.preserve_quotes = True

VERSION = 1
ALODIR = os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))
WORKINGDIR = os.path.abspath(os.path.dirname(__file__))
# REST API Endpoints
BASE_URI = 'api/v1/'
# 0. 로그인
LOGIN = BASE_URI + 'auth/static/login' # POST
# 1. 시스템 정보 획득
SYSTEM_INFO = BASE_URI + 'workspaces' # GET
# 2. AI Solution 이름 설정 / 3. AI Solution 등록
AI_SOLUTION = BASE_URI + 'solutions' # 이름 설정 시 GET, 등록 시 POST
# 4. AI Solution Instance 등록
SOLUTION_INSTANCE = BASE_URI + 'instances' # POST
# 5. Stream 등록
STREAMS = BASE_URI + 'streams' # POST
# 6. Train pipeline 요청
# STREAMS + '/{stream_id}/start # POST
# 7. Train pipeline 확인
# STREAMS + '/{stream_history_id}/info # GET
# 9.a Stream 삭제 
# STREAMS + '/{stream_id} # DELETE
# 9.b AI Solution Instance 삭제
# SOLUTION_INSTANCES + '/{instance_id}' # DELETE
# 9.c AI Solution 삭제
# AI_SOLUTION + '/{solution_id}' # DELETE 
#################################

class SMC:
    def __init__(self, workspaces, uri_scope, tag, name, pipeline):
        self.sm_yaml = {}
        self.ex_yaml = {}
        self.uri_scope = uri_scope
        try:
            self.bucket_name = workspaces.json()[0]['s3_bucket_name'][uri_scope] # bucket_scope: private, public
            self.ecr = workspaces.json()[0]['ecr_base_path'][uri_scope]
        except:
            self.bucket_name = "acp-kubeflow-lhs-s3"
            self.ecr = "086558720570.dkr.ecr.ap-northeast-2.amazonaws.com/acp-kubeflow-lhs/"

        print(self.ecr)
        print(">> bucket name: ", self.bucket_name)

        # FIXME sm.set_aws_ecr 할 때 boto3 session 생성 시 region 을 None으로 받아와서 에러나므로 일단 임시로 추가 
        self.region = "ap-northeast-2"
        self.name = name
        self.pipeline = pipeline
        self.s3_access_key_path = "/nas001/users/ruci.sung/aws.key"
        
    
    def save_yaml(self):
        # YAML 파일로 데이터 저장
        class NoAliasDumper(Dumper):
            def ignore_aliases(self, data):
                return True

        with open('solution_metadata.yaml', 'w', encoding='utf-8') as yaml_file:
            yaml.dump(self.sm_yaml, yaml_file, allow_unicode=True, default_flow_style=False, Dumper=NoAliasDumper)
            

    def set_yaml(self, version=VERSION):
        self.sm_yaml['version'] = version
        self.sm_yaml['name'] = ''
        self.sm_yaml['description'] = {}
        self.sm_yaml['pipeline'] = []
        self.sm_yaml['pipeline'].append({'type': 'train'})
        self.sm_yaml['pipeline'].append({'type': 'inference'})

        self.save_yaml()
        print(f"\n>> solution metadata 작성을 시작합니다. 현재 버전 v{version} 입니다.")
        

    def read_yaml(self, yaml_file_path):
        try:
        # YAML 파일을 읽어옵니다.
            with open(yaml_file_path, 'r') as yaml_file:
                data = yaml.safe_load(yaml_file)

        # 파싱된 YAML 데이터를 사용합니다.
        except FileNotFoundError:
            print(f'File {yaml_file_path} not found.')
        
        if  'solution' in yaml_file_path:
            self.sm_yaml = data
        elif 'experimental' in yaml_file_path:
            self.ex_yaml = data
            if self.ex_yaml['control'][0]['get_asset_source'] == 'every':
                self.ex_yaml['control'][0]['get_asset_source'] = 'once'
            with open(yaml_file_path, 'w') as file:
                yaml.safe_dump(self.ex_yaml, file)
        else:
            pass

    def set_sm_name(self, name):
        self.name = name.replace(" ", "-")
        self.sm_yaml['name'] = self.name

    # {'title': '', 'overview': '', 'input_data': '', 'output_data': '', 'user_parameters': '', 'algorithm': '', 'icon': None}

    def set_sm_description(self, title, overview, input_data, output_data, user_parameters, algorithm, icon):
        self.sm_yaml['description']['title'] = self._check_parammeter(title)
        self.set_sm_name(self._check_parammeter(title))
        self.sm_yaml['description']['overview'] = self._check_parammeter(overview)
        self.sm_yaml['description']['input_data'] = self._check_parammeter(self.bucket_name + input_data)
        self.sm_yaml['description']['output_data'] = self._check_parammeter(self.bucket_name + input_data)
        self.sm_yaml['description']['user_parameters'] = self._check_parammeter(user_parameters)
        self.sm_yaml['description']['algorithm'] = self._check_parammeter(algorithm)
        # FIXME icon 관련 하드코딩 변경필요 
        self.sm_yaml['description']['icon'] = self.icon_s3_uri  #self._check_parammeter(icon)
        self.save_yaml()
        print("solution metadata description이 작성되었습니다")

    def set_wrangler(self):
        self.sm_yaml['wrangler_code_uri'] = ''
        self.sm_yaml['wrangler_dataset_uri'] = ''
        self.save_yaml()

    def set_container_uri(self, type):
        if type == 'train':
            data = {'container_uri': self.ecr_full_url}
            self.sm_yaml['pipeline'][0].update(data)
            print(f"container uri is {data['container_uri']}")
        elif type == 'inf' or type == 'inference':
            data = {'container_uri': self.ecr_full_url}
            self.sm_yaml['pipeline'][1].update(data)
            print(f"container uri is {data['container_uri']}")
        self.save_yaml()

    #s3://s3-an2-cism-dev-aic/artifacts/bolt_fastening_table_classification/train/artifacts/2023/11/06/162000/
    def set_artifacts_uri(self, pipeline):
        data = {'artifact_uri': "s3://" + self.bucket_name + "/artifact/" + self.name + "/" + pipeline + "/" + "artifacts/"}
        if pipeline == 'train':
            self.sm_yaml['pipeline'][0].update(data)
        if pipeline == 'inf' or 'inference':
            self.sm_yaml['pipeline'][1].update(data)
        self.save_yaml()
        print(f"{data['artifact_uri']} were stored")

    def set_model_uri(self, pipeline):
        data = {'model_uri': "s3://" + self.bucket_name + "/artifact/" + self.name + "/" + pipeline + "/" + "artifacts/"}
        if pipeline == 'train':
            print("not support this type")
        if pipeline == 'inf' or 'inference':
            self.sm_yaml['pipeline'][1].update(data)
        self.save_yaml()
        print(f"{data['model_uri']} were stored")

    # FIXME hardcoding 고쳐야함 (image, table)
    def set_edge(self):
        self.sm_yaml['edgeconductor_interface'] = {
            'support_labeling': True,
            'inference_result_datatype': 'table', # mql 
            'train_datatype': 'table'
        }

        self.sm_yaml['edgeapp_interface'] = {'redis_server_uri': ""}
        self.save_yaml()


    def set_train_dataset_uri(self, uri):
        pass

    def set_train_artifact_uri(self, uri):
        pass

    def set_cadidate_param(self, pipeline):
        yaml_path = "../../config/experimental_plan.yaml"
        self.read_yaml(yaml_path)

        

        def rename_key(d, old_key, new_key):
            if old_key in d:
                d[new_key] = d.pop(old_key)
        
        if "train" in pipeline:
            temp_dict = self.ex_yaml['user_parameters'][0]
            rename_key(temp_dict, 'train_pipeline', 'candidate_parameters')
            self.sm_yaml['pipeline'][0].update({'parameters' : temp_dict})
        elif "inference" in pipeline:
            temp_dict = self.ex_yaml['user_parameters'][1]
            rename_key(temp_dict, 'inference_pipeline', 'candidate_parameters')
            self.sm_yaml['pipeline'][1].update({'parameters' : temp_dict})
    
        subkeys = {}
        output_datas = []
        for step in temp_dict['candidate_parameters']:
            output_data = {'step': step['step'], 'args': []}
            output_datas.append(output_data)
        
        subkeys['user_parameters'] = output_datas
        subkeys['selected_user_parameters'] = output_datas
        
        if "train" in pipeline:
            self.sm_yaml['pipeline'][0]['parameters'].update(subkeys)
        elif "inference" in pipeline:
            self.sm_yaml['pipeline'][1]['parameters'].update(subkeys)

        print("candidate_parameters were stored")
        
        self.save_yaml()

    def set_resource(self, pipeline, resource = 'standard'):
        if "train" in pipeline:
            self.sm_yaml['pipeline'][0]["resource"] = {"default": resource}
        elif "inference" in pipeline:
            self.sm_yaml['pipeline'][1]["resource"] = {"default": resource}

        print(f"cloud resource was {resource}")
        self.save_yaml()

    def s3_access_check(self):
        self.s3_path = f'ai-solutions/{self.name}/v{VERSION}/{self.pipeline}/data'
        
        
        try:
            f = open(self.s3_access_key_path, "r")
            keys = []
            values = []
            for line in f:
                key = line.split(":")[0]
                value = line.split(":")[1].rstrip()
                keys.append(key)
                values.append(value)
            ACCESS_KEY = values[0]
            SECRET_KEY = values[1]

            self.s3 = boto3.client('s3',
                                aws_access_key_id=ACCESS_KEY,
                                aws_secret_access_key=SECRET_KEY)
        except:
            self.s3 = boto3.client('s3')

        # FIXME 아래 region이 none으로 나옴 
        # my_session = boto3.session.Session()
        # self.region = my_session.region_name

        print(">> access check: ", isinstance(boto3.client('s3'), botocore.client.BaseClient))
        print(">> my region: ", self.region)
        return isinstance(boto3.client('s3'), botocore.client.BaseClient)
    
            
    def s3_upload_icon(self):
  
        # inner func.
        def s3_process(s3, bucket_name, data_path, s3_path):
            print(">> Upload bucket + s3 path: ", bucket_name, s3_path)
            objects_to_delete = s3.list_objects(Bucket=bucket_name, Prefix=s3_path)

            if 'Contents' in objects_to_delete:
                for obj in objects_to_delete['Contents']:
                    self.s3.delete_object(Bucket=bucket_name, Key=obj['Key'])
                    print(f'>> Deleted pre-existing object: {obj["Key"]}')

            s3.delete_object(Bucket=bucket_name, Key=s3_path)
            s3.put_object(Bucket=bucket_name, Key=(s3_path +'/'))

            try:    
                response = s3.upload_file(data_path, bucket_name, s3_path)
            except NoCredentialsError:
                print("NoCredentialsError")
            except ClientError as e:
                print(f"ClientError{e}")
                return False
            print(f">> [Success Uploading] S3 {bucket_name + s3_path} ")
            return True

        # FIXME hardcoding icon.png 솔루션 이름등으로 변경 필요 
        data_path = './image/icon.png'
        s3_file_path = f'icons/{self.name}/icon.png'
        s3_process(self.s3, self.bucket_name, data_path, s3_file_path)
        
        self.icon_s3_uri = "s3://" + self.bucket_name + '/' + s3_file_path   # 값을 리스트로 감싸줍니다
        self.sm_yaml['description']['icon'] = self.icon_s3_uri
    
        self.save_yaml()
        


    def s3_upload(self, pipeline):
        self.pipeline = pipeline

        # inner func.
        def s3_process(s3, bucket_name, data_path, local_folder, s3_path):
            print(">> Upload bucket + s3 path: ", bucket_name, s3_path)
            objects_to_delete = s3.list_objects(Bucket=bucket_name, Prefix=s3_path)

            if 'Contents' in objects_to_delete:
                for obj in objects_to_delete['Contents']:
                    self.s3.delete_object(Bucket=bucket_name, Key=obj['Key'])
                    print(f'>> Deleted pre-existing object: {obj["Key"]}')

            s3.delete_object(Bucket=bucket_name, Key=s3_path)
            
            s3.put_object(Bucket=bucket_name, Key=(s3_path +'/'))

            try:    
                response = s3.upload_file(data_path, bucket_name, s3_path + "/" + data_path[len(local_folder):])
            except NoCredentialsError:
                print("NoCredentialsError")
            except ClientError as e:
                print(f"ClientError{e}")
                return False
            
            temp = s3_path + "/" + data_path[len(local_folder):]
            print(f">> [Success Uploading] S3 {temp}")
            
            return True

        if "train" in self.pipeline:
            # local_folder = '../../input/train/'
            local_folder = ALODIR+"/input/train/"
            for root, dirs, files in os.walk(local_folder):
                for file in files:
                    data_path = os.path.join(root, file)
            print(f'from local data folder: {local_folder}')
            s3_process(self.s3, self.bucket_name, data_path, local_folder, self.s3_path)
            # self.sm_yaml['pipeline'].append({'dataset_uri': 'train'})

            data = {'dataset_uri': ["s3://" + self.bucket_name + "/" + self.s3_path + "/"]}  # 값을 리스트로 감싸줍니다
            self.sm_yaml['pipeline'][0].update(data)
            
            # data = {'dataset_uri': "s3://" + self.bucket_name + s3_path + "/"}
            # self.sm_yaml['pipeline'][0].update(data)
            self.save_yaml()
            
        elif "inf" in self.pipeline:
            local_folder = ALODIR+"/input/inference/"
            for root, dirs, files in os.walk(local_folder):
                for file in files:
                    data_path = os.path.join(root, file)
            
            s3_path = f'solution/{self.name}/inference/data'
            s3_process(self.s3, self.bucket_name, data_path, local_folder, s3_path)
            
            data = {'dataset_uri': ["s3://" + self.bucket_name + "/" + s3_path + "/"]}  # 값을 리스트로 감싸줍니다
            self.sm_yaml['pipeline'][1].update(data)
            
            # data = {'dataset_uri': "s3://" + self.bucket_name + s3_path + "/"}
            # self.sm_yaml['pipeline'][1].update(data)
            self.save_yaml()
        else:
            print(f"{self.pipeline}은 지원하지 않는 pipeline 구조 입니다")

    def get_contents(self, url):
        def _is_git_url(url):
            git_url_pattern = r'^(https?|git)://[^\s/$.?#].[^\s]*$'
            return re.match(git_url_pattern, url) is not None

        contents_path = "./contents"
        if(_is_git_url(url)):
        
            if os.path.exists(contents_path):
                shutil.rmtree(contents_path)  # 폴더 제거
            repo = git.Repo.clone_from(url, "./contents")

    def set_alo(self):
        alo_path = ALODIR
        alo_src = ['/main.py', '/src', '/config', '/assets', '/alolib']
        work_path = WORKINGDIR + "/alo/"

        if os.path.isdir(work_path):
            shutil.rmtree(work_path)
        os.mkdir(work_path)

        for item in alo_src:
            # src_path = alo_path + os.path.relpath(item)
            src_path = alo_path + item
            print(src_path)
            if os.path.isfile(src_path):
                shutil.copy2(src_path, work_path)
            elif os.path.isdir(src_path):
                shutil.copytree(src_path, work_path + '/' + os.path.basename(src_path))
        
        print(">> Success ALO setting.")

    def set_docker_contatiner(self):
        
        dockerfile = "/Dockerfile"
        if os.path.isfile(WORKINGDIR + dockerfile):
            os.remove(WORKINGDIR + dockerfile)
        
        shutil.copy(WORKINGDIR + "/origin/" + dockerfile, WORKINGDIR)
        file_path = WORKINGDIR + dockerfile
        
        spm = 'ENV SOLUTION_PIPELINE_MODE='

        d_file = []

        with open(file_path, 'r') as file:
            for line in file:
                if line.startswith(spm):
                    if line.find(self.pipeline) > 0:
                        # 현재 파이프라인으로 구동
                        pass
                    else:
                        # 다른 파이프라인으로 dockerfile을 수정 후 구동
                        line = line.replace('train', self.pipeline)
                d_file.append(line)
        data = ''.join(d_file)
        with open(file_path, 'w') as file:
            file.write(data)

        print(">> Success DOCKERFILE setting.")

    def set_aws_ecr(self, docker = True, tags = {}):
        self.docker = docker
        self.ecr_url = self.ecr.split("/")[0]
        self.ecr_repo = self.ecr.split("/")[1] + "/ai-solution/" + self.uri_scope + "/" + self.name + "/" + self.pipeline + "/"  + self.name  #+ ":" + tag
        self.ecr_full_url = self.ecr_url + '/' + self.ecr_repo
        
        if self.docker:
            run = 'docker'
        else:
            run = 'buildah'

        print(">> region: ", self.region)
        p1 = subprocess.Popen(
            ['aws', 'ecr', 'get-login-password', '--region', f'{self.region}'], stdout=subprocess.PIPE
        )
        print(">> ecr url: ", self.ecr_url)
        p2 = subprocess.Popen(
            [f'{run}', 'login', '--username', 'AWS','--password-stdin', f'{self.ecr_url}'], stdin=p1.stdout, stdout=subprocess.PIPE
        )

        p1.stdout.close()
        output = p2.communicate()[0]
        print(output.decode())
        # subprocess.run("echo ws.jang | sudo -S usermod -aG docker $USER", shell=True)
        # subprocess.run("echo ws.jang | newgrp docker", shell=True)
        # subprocess.run(f'aws ecr get-login-password --region {self.region}  | docker login --username AWS --password-stdin {self.ecr_url}', shell=True)
        #subprocess.run("echo ws.jang | sudo aws ecr get-login-password --region ap-northeast-2  | sudo docker login --username AWS --password-stdin 086558720570.dkr.ecr.ap-northeast-2.amazonaws.com", shell=True)
        print(">> ecr repo: ", self.ecr_repo)
        if len(tags) > 0:
            command = [
            "aws",
            "ecr",
            "create-repository",
            "--region", self.region,
            "--repository-name", self.ecr_repo,
            "--image-scanning-configuration", "scanOnPush=true",
            "--tags"
            ] + tags  # 전달된 태그들을 명령어에 추가합니다.
        else:
            command = [
            "aws",
            "ecr",
            "create-repository",
            "--region", self.region,
            "--repository-name", self.ecr_repo,
            "--image-scanning-configuration", "scanOnPush=true",
            ]
        print('command: ', command)
        # subprocess.run() 함수를 사용하여 명령을 실행합니다.
        try:
            result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            print("명령어 실행 결과:", result.stdout)
        except subprocess.CalledProcessError as e:
            print("오류 발생:", e.stderr)


    
    def build_docker(self, TAG='latest'):
        
        if self.docker:
            subprocess.run(['docker', 'build', '.', '-t', f'{self.ecr_full_url}:{TAG}'])
        else:
            subprocess.run(['sudo', 'buildah', 'build', '--isolation', 'chroot', '-t', f'{self.ecr_full_url}:{TAG}'])

    def docker_push(self, TAG='latest'):

        if self.docker:
            subprocess.run(['docker', 'push', f'{self.ecr_full_url}:{TAG}'])
        else:
            subprocess.run(['sudo', 'buildah', 'push', f'{self.ecr_full_url}:{TAG}'])
        
        if self.docker:
            subprocess.run(['docker', 'logout'])
        else:
            subprocess.run(['sudo', 'buildah', 'logout', '-a'])

    def _check_parammeter(self, param):
        if self._check_str(param):
            return param
        else:
            print("입력하신 내용이 str이 아닙니다. 해당 내용은 빈칸이 들어 갑니다")
            return ""

    def _check_str(self, data):
        return isinstance(data, str)

if __name__ == "__main__":
    # s3_bucket = 'acp-kubeflow-lhs-s3'
    # ecr = "acp-kubeflow-lhs-s3"
    name = 'test2'
    workspaces = [
          {
                "id": "00000000-0000-0000-0000-000000000000",
                "name": "string",
                "namespace": "string",
                "kubeflow_user": "string",
                "s3_bucket_name": "string",
                "ecr_base_path": "string",
                "execution_specs": [
                    {
                        "name": "string",
                        "label": "string",
                        "vcpu": 0,
                        "ram_gb": 0,
                        "gpu": 0
                    }
            ],
            "is_deleted": 0
        }
    ]
    sm = SMC(workspaces=workspaces, name=name)

    os.chdir(WORKINGDIR)
    sm.set_alo()
    
    sm.set_yaml()
    sm.set_sm_description("bolt blalal", "테스트중이다", "s3://하하하", "s3://호호호", "params", "alo", "s3://icon")

    sm.s3_access_check()
    
    pipeline = 'train'
    sm.s3_upload(pipeline)
    sm.set_docker_contatiner()
    
    tags = [
    "Key=Company,Value=LGE",
    "Key=Owner,Value=IC360",
    "Key=HQ,Value=CDO",
    "Key=Division,Value=CDO",
    "Key=Infra Region,Value=KIC",
    "Key=Service Mode,Value=DE",
    "Key=Cost Type,Value=COMPUTING",
    "Key=Project,Value=CIS",
    "Key=Sub Project,Value=CISM",
    "Key=System,Value=AIDX"
]
    
    sm.set_aws_ecr(tags=tags)
    sm.set_container_uri(pipeline) # uri도 그냥 입력되게 수정
    sm.set_cadidate_param(pipeline)
    sm.set_artifacts_uri(pipeline)
    sm.set_resource(pipeline)
    
    pipeline = 'inference'
    sm.s3_upload(pipeline)
    sm.set_container_uri(pipeline)
    sm.set_cadidate_param(pipeline)
    sm.set_artifacts_uri(pipeline)
    sm.set_resource(pipeline)

    sm.set_wrangler()