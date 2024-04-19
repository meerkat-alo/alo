# docker 제작시 모든 step 패키지를 설치하게 수정
import docker
from docker.errors import APIError, BuildError
import sys
import time
import boto3
import os
import re
import git
import shutil
import datetime
import yaml 
from yaml import Dumper
import botocore
from botocore.exceptions import ProfileNotFound, ClientError, NoCredentialsError
import subprocess
# 모듈 import 
import os
import json
import requests
import shutil
import tarfile
import docker
from copy import deepcopy 
from pprint import pprint
import configparser
import pyfiglet

### internal package 
from src.constants import *
from src.external import S3Handler
from src.utils import print_color

#----------------------------------------#
#              REST API                  #
#----------------------------------------#

KUBEFLOW_STATUS = ("pending", "running", "succeeded", "skipped", "failed", "error")
#---------------------------------------------------------

class SolutionRegister:
    # class const 변수
    SOLUTION_FILE = '.response_solution.json'
    SOLUTION_INSTANCE_FILE = '.response_solution_instance.json'
    STREAM_FILE = '.response_stream.json'
    STREAM_RUN_FILE = '.response_stream_run.json'
    STREAM_STATUS_FILE = '.response_stream_status.json'

    STREAM_HISTORY_LIST_FILE = '.response_stream_history_list.json'
    STREAM_LIST_FILE = '.response_stream_list.json'
    INSTANCE_LIST_FILE = '.response_instance_list.json'
    SOLUTION_LIST_FILE = '.response_solution_list.json'
    #def __init__(self, workspaces, uri_scope, tag, name, pipeline):
    def __init__(self, infra_setup=None, solution_info=None, experimental_plan=None):
        """ 등록에 필요한 정보들을 입력 받습니다. 
            - infra_setup (str or dict): str 일 경우, path 로 인지하여 file load 하여 dict 화 함 
            - solution_info (str or dict): str 일 경우, path 로 인지하여 file load 하여 dict 화 함 

            - experimental_plan (str or dict):  str 일 경우, path 로 인지하여 file load 하여 dict 화 함

        """

        self.sol_reg_index = 0
        
        self.print_step("Initiate ALO operation mode")
        print_color("[SYSTEM] Solutoin 등록에 필요한 setup file 들을 load 합니다. ", color="green")
        
        def check_and_load_yaml(path_or_dict, mode=''):
            if not mode in ['infra_setup', 'solution_info', 'experimental_plan']:
                raise ValueError("The mode must be infra_setup, solution_info, or experimental_plan. (type: {})".format(type))
            
            if path_or_dict == None or path_or_dict == '' :
                if mode == 'infra_setup': path = DEFAULT_INFRA_SETUP
                elif mode == 'solution_info': path = DEFAULT_SOLUTION_INFO
                else: path = DEFAULT_EXP_PLAN
                print(f"{mode} 파일이 존재 하지 않으므로, Default 파일을 load 합니다. (path: {path})")
                try:    
                    with open(path) as f:
                        result_dict = yaml.safe_load(f)
                except Exception as e : 
                    raise ValueError(str(e))
            else:
                if isinstance(path_or_dict, str):
                    print(f"{mode} 파일을 load 합니다. (path: {path_or_dict})")
                    try:    
                        with open(path_or_dict) as f:
                            result_dict = yaml.safe_load(f)
                    except Exception as e : 
                        raise ValueError(str(e))
                elif isinstance(path_or_dict, dict):
                    result_dict = path_or_dict
                else:
                    raise ValueError(f"{mode} 파일이 유효하지 않습니다. (infra_setup: {path_or_dict})")
            return result_dict

        self.infra_setup = check_and_load_yaml(infra_setup, mode='infra_setup')
        self.solution_info = check_and_load_yaml(solution_info, mode='solution_info')
        self.exp_yaml = check_and_load_yaml(experimental_plan, mode='experimental_plan')

        print_color("[SYSTEM] infra_setup (max display: 5 line): ", color='green')
        pprint(self.infra_setup, depth=5)

        ####################################
        ########### Setup aws key ##########
        ####################################
        # s3_client = S3Handler(s3_uri=None, aws_key_profile=self.infra_setup["AWS_KEY_PROFILE"])
        # self.aws_access_key = s3_client.access_key
        # self.aws_secret_key = s3_client.secret_key

        ####################################
        ########### Setup AIC api ##########
        ####################################
        file_name = SOURCE_HOME + 'config/ai_conductor_api.json'

        # 파일로부터 JSON 데이터를 읽기
        with open(file_name, 'r') as file:
            data = json.load(file)
        
        def convert_to_float(input_str):
            try:
                float_value = float(input_str)  
                return float_value
            except ValueError:
                pass

        def find_max_value(input_list):
            max_value = None
            for item in input_list:
                converted_item = convert_to_float(item)
                if isinstance(converted_item, float):
                    if max_value is None or converted_item > max_value:
                        max_value = converted_item
            return max_value
        
        self.api_uri = data['API']
        ## legacy 버전 보다 낮을 경우, API 변경
        self.api_uri_legacy_version = 1.5
        version = self.check_version()
        max_val = find_max_value(list(data.keys()))

        if version >max_val:
            version = max_val
        
        self.register_solution_api = data[f'{version}']['REGISTER_SOLUTION']
        self.register_solution_instance_api = data[f'{version}']['REGISTER_SOLUTION_INSTANCE']
        self.register_stream_api = data[f'{version}']['REGISTER_STREAM']
        self.request_run_stream_api = data[f'{version}']['REQUEST_RUN_STREAM']

        self.api_uri_legacy = {
            'STATIC_LOGIN': 'api/v1/auth/static/login',  # POST
        }

        ####################################
        ########### Configuration ##########
        ####################################

        ## internal variables
        self.sm_yaml = {}  ## core

        self.pipeline = None 
        self.aic_cookie = None
        self.solution_name = None
        self.icon_filenames = []
        self.sm_pipe_pointer = -1  ## 0 부터 시작
        self.resource_list = []

        self.bucket_name = None
        self.bucket_name_icon = None 
        self.ecr_name= None
        self.solution_version_new = 1   # New 가 1 부터 이다. 
        self.solution_version_id = None  # solution update 에서만 사용

        ## debugging 용 변수
        self.debugging = False 
        self.skip_generation_docker = False

        def make_art(str):
            terminal_width = shutil.get_terminal_size().columns
            ascii_art = pyfiglet.figlet_format(str, font="slant")
            centered_art = '\n'.join(line.center(terminal_width) for line in ascii_art.splitlines())
            print("*" * 80)
            print(ascii_art)
            print("*" * 80)
        
        make_art("Register AI Solution!!!")

        self._s3_access_check()

    ################################################
    ################################################
    def set_solution_settings(self):
        self.check_solution_name()
        self.load_system_resource()   ## ECR, S3 정보 받아오기

    def set_solution_metadata(self):
        #solme
        self._init_solution_metadata()
        self._set_alo()  ## contents_name 확인용
        self.set_description()
        self.select_icon(name='ic_artificial_intelligence')
        self.set_wrangler()
        self.set_edge()
    
    def run_pipelines(self, pipes):
        self._sm_append_pipeline(pipeline_name=pipes) # sm
        self.set_resource(resource='high')  ## resource 선택은 spec-out 됨
        self.set_user_parameters() # sm
        self.s3_upload_data() # s3
        self.s3_upload_artifacts() #s3
        if (not self.debugging) and (not self.skip_generation_docker):
            skip_build=False
        else:
            skip_build=True
        codebuild_client, build_id = self.make_docker(skip_build)
        self.docker_push()
        print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
        self.sol_reg_index = self.sol_reg_index + 1
        self._set_container_uri()
        return codebuild_client, build_id

    ################################################
    ################################################

    def run(self):
        #############################
        ###  Solution Name 입력
        #############################
        self.set_solution_settings()

        #############################
        ### description & wranlger 추가 
        #############################
        self.set_solution_metadata()

        #############################
        ### contents type 추가 
        #############################
        ## common
          ## s3 instance 생성 
        self.set_resource_list()
        # codebuild로 실행시 client와 id를 추후 받아옴
        # train_codebuild_client, train_build_id = None, None
        # inference_codebuild_client, inference_build_id = None, None
        ############################
        ### Train pipeline 설정
        ############################
        pipelines = ["train", "inference"]

        for pipes in pipelines:
            if self.solution_info['inference_only'] and pipes == 'train':
                continue
            codebuild_client, build_id= self.run_pipelines(pipes)
            if codebuild_client != None and build_id != None: 
                self._batch_get_builds(codebuild_client, build_id, status_period=20)
        
        if not self.debugging:
            # solution 등록 전이라 solution 삭제 코드가 들어갈 수 없음
            self.register_solution()
            # solution instance 등록 전이라 solution instance 삭제 코드가 들어갈 수 없음
            # solution instance 등록이 실패 하면 solution 삭제
            self.register_solution_instance()   ## AIC, Solution Storage 모두에서 instance 까지 항상 생성한다. 

    def run_train(self, status_period=5, delete_solution=False):
        
        if self.solution_info['inference_only']:
            raise ValueError("inference_only=False 여야 합니다.")
        else:
            self.register_solution_instance()
            self.register_stream()
            self.request_run_stream()
            self.get_stream_status(status_period=status_period)
            # self.delete_stream_history()  ## stream 삭제 시, 자동 삭제 됨
            self.delete_stream()

        if delete_solution:
            self.delete_solution_instance()
            self.delete_solution()

    def print_step(self, step_name, sub_title=False):
        if not sub_title:
            print_color("\n######################################################", color='blue')
            print_color(f'#######    {step_name}', color='blue')
            print_color("######################################################\n", color='blue')
        else:
            print_color(f'\n#######  {step_name}', color='blue')

    def check_version(self):
        """ AI Conductor 의 버전을 확인하여 API 를 변경함
        """
        self.print_step("Check Version", sub_title=True)

        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["VERSION"]
        response = requests.get(aic+api)
        if response.status_code == 200:
            response_json = response.json()
            version_str = response_json['versions'][0]['ver_str']
            print_color(f"[SUCCESS] Version 을 확인 하였습니다. (current_version: {version_str}). ", color='cyan')

            match = re.match(r'(\d+\.\d+)', version_str)
            if match:
                version = float(match.group(1))
            else:
                version = float(version_str)

            if self.api_uri_legacy_version >= version:
                self.api_uri.update(self.api_uri_legacy)

                print_color(f"[INFO] API 의 uri 가 변경되었습니다.", color='yellow')
                pprint(f"changed_uri:{self.api_uri_legacy}")
        elif response.status_code == 400:
            raise ValueError("[ERROR] version 을 확인할 수 없습니다.  ")

        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')

        return version

    def login(self, id, pw): 
        # 로그인 (관련 self 변수들은 set_user_input 에서 setting 됨)

        self.print_step("Login to AI Conductor", sub_title=True)

        login_data = json.dumps({
            "login_id": id,
            "login_pw": pw
        })
        try:
            if self.infra_setup["LOGIN_MODE"] == 'ldap':
                response = requests.post(self.infra_setup["AIC_URI"] + self.api_uri["LDAP_LOGIN"], data = login_data)
                print(response)
            else:
                response = requests.post(self.infra_setup["AIC_URI"] + self.api_uri["STATIC_LOGIN"], data = login_data)
        except Exception as e:
            print(e)

        response_login = response.json()

        cookies = response.cookies.get_dict()
        access_token = cookies.get('access-token', None)
        self.aic_cookie = {
        'access-token' : access_token 
        }

        if response.status_code == 200:
            print_color("[SUCCESS] Login 접속을 성공하였습니다. ", color='cyan')
            # print(f"[INFO] Login response:")
            # pprint(response_login)

            ws_list = []
            for ws in response_login["workspace"]:
                ws_list.append(ws["name"])
            print(f"해당 계정으로 접근 가능한 workspace list: {ws_list}")

            ## 로그인 접속은  계정 존재 / 권한 존재 의 경우로 나뉨
            ##   - case1: 계정 O / 권한 X 
            ##   - case2: 계정 O / 권한 single (ex cism-ws) 
            ##   - case3: 계정 O / 권한 multi (ex cism-ws, magna-ws) -- 권한은 workspace 단위로 부여 
            ##   - case4: 계정 X  ()
            if response_login['account_id']:
                if self.debugging:
                    print_color(f'[SYSTEM] Success getting cookie from AI Conductor:\n {self.aic_cookie}', color='green')
                    print_color(f'[SYSTEM] Success Login: {response_login}', color='green')
                if self.infra_setup["WORKSPACE_NAME"] in ws_list:
                    msg = f'[SYSTEM] 접근 요청하신 workspace ({self.infra_setup["WORKSPACE_NAME"]}) 은 해당 계정으로 접근 가능합니다.'
                    print_color(msg, color='green')
                else:
                    ws_name = self.infra_setup["WORKSPACE_NAME"]
                    msg = f'List of workspaces accessible by the account: {ws_name}) 은 해당 계정으로 접근 불가능 합니다.'
                    raise ValueError(msg)
            else: 
                print_color(f'\n>> Failed Login: {response_login}', color='red')   
        elif response.status_code == 401:
            print_color("[ERROR] login 실패. 잘못된 아이디 또는 비밀번호입니다.", color='red')
            
            if response_login['detail']['error_code'] == 'USER.LOGIN.000':
                pass
            if response_login['detail']['error_code'] == 'USER.LOGIN.001':
                if 'login_fail_count' in list(response_login['detail'].keys()):
                    count = response_login['detail']['login_fail_count']
                    print(f"Password를 오기입하셨습니다 {count} / 5 번 남았습니다.")
                else:
                    print(f"{id}를 오기입 하셨습니다") # id 를 이용
            if response_login['detail']['error_code'] == 'USER.LOGIN.002':
                if 'login_fail_count' in list(response_login['detail'].keys()):
                    if int(response_login['detail']['login_fail_count']) == 5:
                        print(f"5번 잘못 입력하셨습니다. 계정이 잠겼으니 관리자에게 문의 하세요.")
            if response_login['detail']['error_code'] == 'USER.LOGIN.003':
                if 'unused_period' in list(response_login['detail'].keys()):
                    up = response_login['detail']['unused_period']
                    print(f'{up} 만큼 접속하지 않으셔서 계정이 잠겼습니다.')
                    print(f'관리자에게 문의하세요.')
                    
        elif response.status_code == 400:
            print_color("[ERROR] AI Solution 등록을 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", self.response_solution["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] AI Solution 등록을 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", self.response_solution["detail"])
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
    
    def check_solution_name(self, name=None): 
        """사용자가 등록할 솔루션 이름이 사용가능한지를 체크 한다. 

        중복된 이름이 존재 하지 않으면, 신규 솔루션으로 인식한다. 
        만약, 동일 이름 존재하면 업데이트 모드로 전환하여 솔루션 업데이트를 실행한다. 

        Attributes:
            - name (str): solution name

        Return:
            - solution_name (str): 내부 처리로 변경된 이름 
        """
        self.print_step("Solution Name Creation")

        if not name:
            name = self.solution_info['solution_name']
        
        ########## name-rule ###########
        ## 1) 중복 제거를 위해 workspace 이름 추가
        name = name +  "-" + self.infra_setup["WORKSPACE_NAME"]

        # 2) 문자열의 바이트 길이가 100바이트를 넘지 않는지 확인
        if len(name.encode('utf-8')) > 100:
            raise ValueError("The solution name must be less than 50 bytes.")   
        
        # 3) 스페이스, 특수 문자, 한글 제외한 영문자와 숫자만 허용하는 정규 표현식 패턴
        pattern = re.compile('^[a-zA-Z0-9-]+$')
        # 정규 표현식으로 입력 문자열 검사
        if not pattern.match(name):
            raise ValueError("The solution name can only contain lowercase letters / dash / number (ex. my-solution-v0)")

        ########## name-unique #########
        solution_data = {
            "workspace_name": self.infra_setup["WORKSPACE_NAME"], 
            "only_public": 0,  # 1: public, private 다 받아옴, 0: ws 것만
            "page_size": 100
        }
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["SOLUTION_LIST"]
        response = requests.get(aic+api, params=solution_data, cookies=self.aic_cookie)
        response_json = response.json()

        solution_list = []
        if 'solutions' in response_json.keys(): 
            for sol in response_json['solutions']: 
                solution_list.append(sol['name'])

                ### 솔루션 업데이트 
                if self.solution_info['solution_update']:
                    if name in sol['name']: 
                        txt = f"[SUCCESS] The solution name ({name}) already exists and can be upgraded. " 
                        self.solution_name = name
                        self.solution_version_new = int(sol['versions'][0]['version']) + 1  ## latest 확인 때문에 [0] 번째 확인
                        self.solution_version_id = sol['id']
                        print_color(txt, color='green')
        else: 
            msg = f"<< solutions >> key not found in AI Solution data. API_URI={aic+api}"
            raise ValueError(msg)

        ## 업데이트  에러 처리 및 신규 등록 처리 (모든 solution list 검수 후 진행 가능)
        if self.solution_info['solution_update']:
            if not name in solution_list:
                txt = f"[ERROR] if solution_update is True, the same solution name cannot exist.(name: {name})"
                print_color(txt, color='red')
                raise ValueError("Not found solution name.")
        else:
            # 기존 solution 존재하면 에러 나게 하기 
            if name in solution_list:
                txt = f"[SYSTEM] the solution name ({name}) already exists in the AI solution list. Please enter another name !!"
                print_color(txt, color='red')
                raise ValueError("Not allowed solution name.")
            else:  ## NEW solutions
                txt = f"[SUCCESS] the solution name ({name}) is available." 
                self.solution_name = name
                self.solution_version_new = 1
                self.solution_version_id = None
                print_color(txt, color='green')

        msg = f'[INFO] solution name list (workspace: {self.infra_setup["WORKSPACE_NAME"]}):'
        print(msg)
        # 이미 존재하는 solutino list 리스트업 
        # pre_existences = pd.DataFrame(solution_list, columns=["AI solutions"])
        # pre_existences = pre_existences.head(100)
        # print_color(pre_existences.to_markdown(tablefmt='fancy_grid'), color='cyan')
        print_color('< Pre-existing AI Solutions >', color='cyan')
        for idx, sol in enumerate(solution_list): 
            print_color(f'{idx}. {sol}', color='cyan')

        print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
        self.sol_reg_index = self.sol_reg_index + 1
        
    def set_description(self, description={}):
        """솔루션 설명을 solution_metadata 에 삽입합니다. 

        Attributes:
          - desc (dict): title, overview, input_data (format descr.), output_data (format descr.),
          user_parameters, algorithm 에 대한 설명문을 작성한다. 
          추후 mark-up 지원여부 
        """

        self.print_step("Set AI Solution Description")

        ## icon 은 icon 함수에서 삽입
        ## contents_name, contents_version 은 experimental_plan 에서 자동으로 가져오기  (v2.3.0 신규, 24.03.25)
        def _add_descr_keys(d):
            required_keys = ['title', 'problem', 'data', 'key_feature', 'value', 'note_and_contact']

            for key in required_keys:
              if key not in d:
                d[key] = ""
        
        _add_descr_keys(description)
        description['title'] = self.solution_name  ## name 을 title default 로 설정함
        description['contents_name'] = self.exp_yaml['name']
        description['contents_version'] = str(self.exp_yaml['version'])

        try: 
            self.sm_yaml['description'].update(description)
            self._save_yaml()

            print_color(f"[SUCCESS] Update solution_metadata.yaml.", color='green')
            print(f"description:")
            pprint(description)
        except Exception as e: 
            raise NotImplementedError(f"Failed to set << description >> in the solution_metadata.yaml \n{str(e)}")
        
        print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
        self.sol_reg_index = self.sol_reg_index + 1

    def set_wrangler(self):
        """wrangler.py 를 solution_metadata 의 code-to-string 으로 반영합니다. 
        ./wrangler/wrangler.py 만 지원 합니다. 

        """

        self.print_step("Set Wrangler", sub_title=True)
        try: 
            with open(REGISTER_WRANGLER_PATH, 'r') as file:
                python_content = file.read()

            self.sm_yaml['wrangler_code_uri'] = python_content
            self.sm_yaml['wrangler_dataset_uri'] = ''
            self._save_yaml()
        except:
            msg = f"[WARNING] wrangler.py 가 해당 위치에 존재해야 합니다. (path: {REGISTER_WRANGLER_PATH})"
            print_color(msg, color="yellow")

            self.sm_yaml['wrangler_code_uri'] = ''
            self.sm_yaml['wrangler_dataset_uri'] = ''
            self._save_yaml()
        
    def set_edge(self, metadata_value={}):
        """Edge Conductor 에서 처리해야 할 key 를 사용자로 부터 입력 받습니다. 
        이를 soluton_metadata 에 반영합니다. 

        Attributes:
          - metadata_value (dict): 'support_labeling', 'inference_result_datatype', 'train_datatype' key 지원

          사용자가 정상적으로 metadata_value 를 설정하였는지를 체크하고, 이를 solution_metadata 에 반영합니다. 
          - support_labeling : True / False (bool)
          - inference_result_datatype: table / image (str) --> inference reult 를 csv, image 로 display 할지
          - train_datatype: table / image (str) --> inference output 의 형태가 csv, image 인지를 선택
        """

        self.print_step("Set Edge Condcutor & Edge App", sub_title=True)
        
        if len(metadata_value) == 0:
            metadata_value = self.solution_info['contents_type']

        def _check_edgeconductor_interface(user_dict):
            ## v2.3.0 NEW:  labeling_column_name  추가 됨
            check_keys = ['support_labeling', 'inference_result_datatype', 'train_datatype', 'labeling_column_name']
            allowed_datatypes = ['table', 'image']
            for k in user_dict.keys(): ## 엉뚱한 keys 존재 하는지 확인
                self._check_parammeter(k)
                if k not in check_keys: 
                    raise ValueError(f"[ERROR] << {k} >> is not allowed for contents_type key. \
                                     (keys: support_labeling, inference_result_datatype, train_datatype) ")
            for k in check_keys: ## 필수 keys 가 누락되었는지 확인
                if k not in user_dict.keys(): 
                    raise ValueError(f"[ERROR] << {k} >> must be in the edgeconductor_interface key list.")

            ## type 체크 및 key 존재 확인
            if isinstance(user_dict['support_labeling'], bool):
                pass
            else: 
                raise ValueError("[ERROR] << support_labeling >> parameter must have boolean type.")

            if user_dict['inference_result_datatype'] not in allowed_datatypes:
                raise ValueError(f"[ERROR] << inference_result_datatype >> parameter must have the value among these: \n{allowed_datatypes}")

            if user_dict['train_datatype'] not in allowed_datatypes:
                raise ValueError(f"[ERROR] << train_datatype >> parameter must have the value among these: \n{allowed_datatypes}")                  

        # edgeconductor interface 
        _check_edgeconductor_interface(metadata_value)

        self.sm_yaml['edgeconductor_interface'] = metadata_value
        
        ### EdgeAPP 관련 부분도 업데이트 함.
        self.sm_yaml['edgeapp_interface'] = {'redis_server_uri': ""}
        self._save_yaml()

        msg = "[SUCCESS] contents_type 을 solution_metadata 에 성공적으로 업데이트 하였습니다."
        print_color(msg, color="green")
        print(f"edgeconductor_interfance: {metadata_value}")

    def set_resource_list(self):
        """AI Conductor 에서 학습에 사용될 resource 를 선택 하도록, 리스트를 보여 줍니다. (필수 실행)
        """
        self.print_step(f"Display {self.pipeline} Resource List")

        params = {
            "workspace_name": self.infra_setup["WORKSPACE_NAME"],
            "page_size": 100

        }
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["SYSTEM_INFO"]
        try: 
            response = requests.get(aic+api, params=params, cookies=self.aic_cookie)
            response_json = response.json()
        except: 
            raise NotImplementedError("[ERROR] Failed to get workspaces info.")

        resource_list = []
        try: 
            # df = pd.DataFrame(response_json["specs"])
            for spec in response_json["specs"]:
                resource_list.append(spec["name"])
        except: 
            raise ValueError("Got wrong workspace info.")
        print(f"{self.pipeline} Available resource list (type{resource_list}):")
        # print_color(df.to_markdown(tablefmt='fancy_grid'), color='cyan')
        print_color(f"< Allowed Specs >", color='cyan')
        print(response_json["specs"])
        ## 해당 함수는 두번 call 될 수 있기 때문에, global 변수로 관리함
        self.resource_list = resource_list
        return resource_list
        

    def set_resource(self, resource= ''):
        """AI Conductor 에서 학습에 사용될 resource 를 선택 합니다. 
        사용자에게 선택 하도록 해야 하며, low, standard, high 과 같은 추상적 선택을 하도록 합니다.

        Attributes:
          - resource (str): 
        """
        self.print_step(f"Set {self.pipeline} Resource")
        if len(self.resource_list) == 0: # Empty List
            msg = f"[ERROR] set_resource_list 함수를 먼저 실행 해야 합니다."
            raise ValueError(msg)
        
        if resource == '': # erorr when input is empty 
            msg = f"[ERROR] 입력된 {self.pipeline}_resource 가 empty 입니다. Spec 을 선택해주세요. (type={self.resource_list})"
            raise ValueError(msg)

        if not resource in self.resource_list:
            msg = f"[ERROR] 입력된 {self.pipeline}_resource 가 '{resource}' 입니다. 미지원 값입니다. (type={self.resource_list})"
            raise ValueError(msg)

        self.sm_yaml['pipeline'][self.sm_pipe_pointer]["resource"] = {"default": resource}

        ## inference 의 resource 를 선택하는 시나리오가 없음. standard 로 강제 고정 (24.01.17)
        if self.pipeline == "inference":
            self.sm_yaml['pipeline'][self.sm_pipe_pointer]["resource"] = {"default": 'standard'}
            resource = 'standard'
            msg = f"EdgeApp 에 대한 resource 설정은 현재 미지원 입니다. resource=standard 로 고정 됩니다."
            print_color(msg, color="yellow")

        print_color(f"[SUCCESS] Update solution_metadat.yaml:", color='green')
        print(f"pipeline[{self.sm_pipe_pointer}]: -resource: {resource}")
        self._save_yaml()

    def load_system_resource(self): 
        """ 사용가능한 ECR, S3 주소를 반환한다. 
        """

        self.print_step("Check ECR & S3 Resource")
        params = {
            "workspace_name": self.infra_setup["WORKSPACE_NAME"],
            "page_size": 100

        }
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["SYSTEM_INFO"]

        try: 
            response = requests.get(aic+api, params=params, cookies=self.aic_cookie)
            response_json = response.json()
        except: 
            raise NotImplementedError("Failed to get workspaces info.")

        # workspace로부터 받아온 ecr, s3 정보를 내부 변수화 
        try:
            solution_type = self.solution_info["solution_type"]
            self.bucket_name = response_json["s3_bucket_name"][solution_type] # bucket_scope: private, public
            self.bucket_name_icon = response_json["s3_bucket_name"]["public"] # icon 은 공용 저장소에만 존재. = public
            self.ecr_name = response_json["ecr_base_path"][solution_type]
        except Exception as e:
            raise ValueError(f"Wrong format of << workspaces >> received from REST API:\n {e}")

        if self.debugging:
            print_color(f"\n[INFO] S3_BUCUKET_URI:", color='green') 
            print_color(f'- public: {response_json["s3_bucket_name"]["public"]}', color='cyan') 
            print_color(f'- private: {response_json["s3_bucket_name"]["public"]}', color='cyan') 

            print_color(f"\n[INFO] ECR_URI:", color='green') 
            print_color(f'- public: {response_json["ecr_base_path"]["public"]}', color='cyan') 
            print_color(f'- private: {response_json["ecr_base_path"]["public"]}', color='cyan') 

            
        print_color(f"[SYSTEM] AWS ECR:  ", color='green') 
        print(f"{self.ecr_name}") 
        print_color(f"[SYSTEM] AWS S3 bucket:  ", color='green') 
        print(f"{self.bucket_name}") 

        print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
        self.sol_reg_index = self.sol_reg_index + 1
    
    def set_pipeline_uri(self, mode, data_paths = [], skip_update=False):
        """ dataset, artifacts, model 중에 하나를 선택하면 이에 맞느 s3 uri 를 생성하고, 이를 solution_metadata 에 반영한다.

        Attributes:
          - mode (str): dataset, artifacts, model 중에 하나 선택

        Returns:
          - uri (str): s3 uri 를 string 타입으로 반환 함 
        """
        if mode == "artifact":
            prefix_uri = "ai-solutions/" + self.solution_name + f"/v{self.solution_version_new}/" + self.pipeline  + "/artifacts/"
            uri = {'artifact_uri': "s3://" + self.bucket_name + "/" + prefix_uri}
        elif mode == "data":
            prefix_uri = "ai-solutions/" + self.solution_name + f"/v{self.solution_version_new}/" + self.pipeline  + "/data/"
            if len(data_paths) ==0 :
                uri = {'dataset_uri': ["s3://" + self.bucket_name + "/" + prefix_uri]}
            else:
                uri = {'dataset_uri': []}
                data_path_base = "s3://" + self.bucket_name + "/" + prefix_uri
                for data_path_sub in data_paths:
                    uri['dataset_uri'].append(data_path_base + data_path_sub)
        elif mode == "model":  ## model
            prefix_uri = "ai-solutions/" + self.solution_name + f"/v{self.solution_version_new}/" + 'train'  + "/artifacts/"
            uri = {'model_uri': "s3://" + self.bucket_name + "/" + prefix_uri}
        else:
            raise ValueError("mode must be one of [data, artifact, model]")

        try: 
            if self.pipeline == 'train':
                if not self.sm_yaml['pipeline'][self.sm_pipe_pointer]['type'] == 'train':
                    raise ValueError("Setting << artifact_uri >> in the solution_metadata.yaml is only allowed for << train >> pipeline. \n - current pipeline: {self.pipeline}")
                ## train pipelne 시에는 model uri 미지원
                if mode == "model":
                    raise ValueError("Setting << model_uri >> in the solution_metadata.yaml is only allowed for << inference >> pipeline. \n - current pipeline: {self.pipeline}")
            else: ## inference
                if not self.sm_yaml['pipeline'][self.sm_pipe_pointer]['type'] == 'inference':
                    raise ValueError("Setting << artifact_uri >> in the solution_metadata.yaml is only allowed for << inference >> pipeline. \n - current pipeline: {self.pipeline}")

            if skip_update:
                pass
            else:
                self.sm_yaml['pipeline'][self.sm_pipe_pointer].update(uri)
                self._save_yaml()

                print_color(f'[SUCCESS] Update solution_metadata.yaml:', color='green')
                if mode == "artifacts":
                    print(f'pipeline: type: {self.pipeline}, artifact_uri: {uri} ')
                elif mode == "data":
                    print(f'pipeline: type: {self.pipeline}, dataset_uri: {uri} ')
                else: ## model
                    print(f'pipeline: type:{self.pipeline}, model_uri: {uri} ')
        except Exception as e: 
            raise NotImplementedError(f"Failed to set << artifact_uri >> in the solution_metadata.yaml \n{str(e)}")
        
        return prefix_uri
    
    def register_solution(self): 
        ''' 일반 등록과 솔루션 업데이트 으로 구분 됨 
        solution_info["solution_update]=True 이면, 업데이트 과정을 진행함
        '''

        self.print_step("Register AI solution")

        try: 
            # 등록을 위한 형태 변경
            self.register_solution_api["scope_ws"] = self.infra_setup["WORKSPACE_NAME"]
            self.register_solution_api["metadata_json"] = self.sm_yaml
            data =json.dumps(self.register_solution_api)

            aic = self.infra_setup["AIC_URI"]
            if self.solution_info["solution_update"]:
                solution_params = {
                    "solution_id": self.solution_version_id,
                    "workspace_name": self.infra_setup["WORKSPACE_NAME"]
                }
                api = self.api_uri["REGISTER_SOLUTION"] + f"/{self.solution_version_id}/version"
            else:
                solution_params = {
                    "workspace_name": self.infra_setup["WORKSPACE_NAME"]
                }
                api = self.api_uri["REGISTER_SOLUTION"]

            # AI 솔루션 등록
            response = requests.post(aic+api, params=solution_params, data=data, cookies=self.aic_cookie)
            self.response_solution = response.json()
        except Exception as e: 
            raise NotImplementedError(f"Failed to register AI solution: \n {str(e)}")

        if response.status_code == 200:
            print_color("[SUCCESS] AI Solution 등록을 성공하였습니다. ", color='cyan')
            print(f"[INFO] AI solution register response: \n {self.response_solution}")

            # interface 용 폴더 생성.
            try:
                if os.path.exists(REGISTER_INTERFACE_PATH):
                    shutil.rmtree(REGISTER_INTERFACE_PATH)
                os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory while registering solution instance: \n {str(e)}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_FILE
            with open(path, 'w') as f:
              json.dump(response.json(), f, indent=4)
              print_color(f"[SYSTEM] register 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] AI Solution 등록을 실패하였습니다. 잘못된 요청입니다. ", color='red')
            raise ValueError("Error message: {}".format(self.response_solution["detail"]))
        elif response.status_code == 422:
            print_color("[ERROR] AI Solution 등록을 실패하였습니다. 유효성 검사를 실패 하였습니다..", color='red')
            raise ValueError("Error message: {}".format(self.response_solution["detail"]))
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
            raise ValueError("URI: {}".format(aic+api))
    
    ################################
    ######    STEP2. S3 Control
    ################################
            
    # def s3_upload_icon_display(self):
    #     """ 가지고 있는 icon 들을 디스플레이하고 파일명을 선택하게 한다. 
    #     """
    #     self.print_step("Display icon list")

    #     # 폴더 내의 모든 SVG 파일을 리스트로 가져오기
    #     svg_files = [os.path.join(REGISTER_ICON_PATH, file) for file in os.listdir(REGISTER_ICON_PATH) if file.endswith('.svg')]

    #     # HTML과 CSS를 사용하여 SVG 파일과 파일명을 그리드 형태로 표시
    #     html_content = '<div style="display: grid; grid-template-columns: repeat(7, 1fr); gap: 10px;">'
    #     icon_filenames = []
    #     for file in svg_files:
    #         file_name = os.path.basename(file)  # 파일 이름만 추출
    #         file_name = file_name.replace(f"{REGISTER_ICON_PATH}/", "")
    #         icon_filenames.append(file_name)
    #         file_name = file_name.replace(f".svg", "")
    #         html_content += f'''<div>
    #                                 <img src="{file}" style="width: 100px; height: 100px;">
    #                                 <div style="word-wrap: break-word; word-break: break-all; width: 100px;">{file_name}</div>
    #                             </div>'''
    #     html_content += '</div>'

    #     self.icon_filenames = icon_filenames

    #     return html_content

    def select_icon(self, name):
        """ icon 업로드는 추후 제공 
        현재는 icon name 을 solution_metadata 에 업데이트 하는 것으로 마무리 
        """
        # self.print_step("select solution icon", sub_title=True )
        if not ".svg" in name:
            name = name+".svg"
        icon_s3_uri = "s3://" + self.bucket_name_icon + '/icons/' + name   # 값을 리스트로 감싸줍니다
        self.sm_yaml['description']['icon'] = icon_s3_uri
        self._save_yaml()
        
    def _s3_access_check(self):
        """ S3 에 접속 가능한지를 확인합니다.  s3_client instance 생성을 합니다.

        1) s3_access_key_path 가 존재하면, 파일에서 key 를 확인하고,
          - TODO file format 공유하기 (프로세스화)
        2) TODO aws configure가 설정되어 있으면 이를 자동으로 해석한다. 
        3) key 없이 권한 설정으로 접속 가능한지도 확인한다. 

        """
        self.print_step("Check to access S3")
        print("**********************************")
        profile_name = self.infra_setup["AWS_KEY_PROFILE"]
        
        try:
            self.session = boto3.Session(profile_name=profile_name)
            self.s3_client = self.session.client('s3', region_name=self.infra_setup['REGION'])
        except ProfileNotFound:
            print_color(f"[WARNING] AWS profile {profile_name} not found. Create session and s3 client without aws profile.", color="yellow")
            self.session = boto3.Session()
            self.s3_client = boto3.client('s3', region_name=self.infra_setup['REGION'])
        except Exception:
            raise ValueError("The aws credentials are not available.")
        
        print_color(f"[INFO] AWS region: {self.infra_setup['REGION']}", color='blue')
        access_check = isinstance(boto3.client('s3', region_name=self.infra_setup['REGION']), botocore.client.BaseClient)
        
        if access_check == True:       
            print_color(f"[INFO] AWS S3 access check: OK", color="green")
        else: 
            raise ValueError(f"[ERROR] AWS S3 access check: Fail")
        return access_check

    def _s3_delete(self, s3, bucket_name, s3_path):
        try: 
            objects_to_delete = s3.list_objects(Bucket=bucket_name, Prefix=s3_path)
            if 'Contents' in objects_to_delete:
                for obj in objects_to_delete['Contents']:
                    self.s3_client.delete_object(Bucket=bucket_name, Key=obj['Key'])
                    print_color(f'[SYSTEM] Deleted pre-existing S3 object: {obj["Key"]}', color = 'yellow')
            s3.delete_object(Bucket=bucket_name, Key=s3_path)
        except: 
            raise NotImplementedError("Failed to s3 delete") 
            
    def _s3_update(self, s3, bucket_name, data_path, local_folder, s3_path):
        s3.put_object(Bucket=bucket_name, Key=(s3_path))
        ## s3 생성
        try:    
            response = s3.upload_file(data_path, bucket_name, s3_path + data_path[len(local_folder):])
        except NoCredentialsError as e:
            raise NoCredentialsError(f"[NoCredentialsError] Failed to create s3 bucket and file upload: \n {str(e)}")
        except Exception as e:
            raise NotImplementedError(f"Failed to create s3 bucket and file upload: \n {str(e)}")
        # temp = s3_path + "/" + data_path[len(local_folder):]
        uploaded_path = bucket_name + '/' + s3_path + data_path[len(local_folder):]
        # print(data_path)
        print_color(f"[SUCCESS] update train_data to S3: \n", color='green')
        print(f"{uploaded_path}")
        return 

    def s3_upload_data(self):
        """input 폴더에 존재하는 데이터를 s3 에 업로드 합니다. 
        """
        self.print_step(f"Upload {self.pipeline} data to S3")
        if "train" in self.pipeline:
            local_folder = INPUT_DATA_HOME + "train/"
            print_color(f'[SYSTEM] Start uploading data into S3 from local folder:\n {local_folder}', color='cyan')
            try: 
                ## sol_metadata 업데이트 
                data_uri_list = []
                for item in os.listdir(local_folder):
                    sub_folder = os.path.join(local_folder, item)
                    if os.path.isdir(sub_folder):
                        data_uri_list.append(item+"/")
                s3_prefix_uri = self.set_pipeline_uri(mode="data", data_paths=data_uri_list)
                ### delete & upload data to S3
                self._s3_delete(self.s3_client, self.bucket_name, s3_prefix_uri) 
                for root, dirs, files in os.walk(local_folder):
                    for file in files:
                        data_path = os.path.join(root, file)
                        self._s3_update(self.s3_client, self.bucket_name, data_path, local_folder, s3_prefix_uri)
            except Exception as e: 
                raise NotImplementedError(f'[ERROR] Failed to upload local data into S3: \n {str(e)}') 
        elif "inference" in self.pipeline:
            local_folder = INPUT_DATA_HOME + "inference/"
            print_color(f'[INFO] Start uploading data into S3 from local folder:\n {local_folder}', color='cyan')
            try: 
                ## sol_metadata 업데이트 
                data_uri_list = []
                for item in os.listdir(local_folder):
                    sub_folder = os.path.join(local_folder, item)
                    if os.path.isdir(sub_folder):
                        data_uri_list.append(item+"/")
                s3_prefix_uri = self.set_pipeline_uri(mode="data", data_paths=data_uri_list)
                ### delete & upload data to S3
                self._s3_delete(self.s3_client, self.bucket_name, s3_prefix_uri) 
                for root, dirs, files in os.walk(local_folder):
                    for file in files:
                        data_path = os.path.join(root, file)
                        self._s3_update(self.s3_client, self.bucket_name, data_path, local_folder, s3_prefix_uri) 
            except Exception as e: 
                raise NotImplementedError(f'[ERROR] Failed to upload local data into S3: \n {str(e)}') 
        else:
            raise ValueError(f"[ERROR] Not allowed value for << pipeline >>: {self.pipeline}")

    def s3_upload_stream_data(self, stream_id='stream_id', instance_id='insatsnce_id'):
        """input 폴더에 존재하는 데이터를 stream 용 s3 에 업로드 합니다. 
            s3_upload_data 를 최적화 만 함. s3_upload_data 를 그대로 사용하기에는 조건문이 많아져서 최적화 힘들것으로 판단
        """
        self.print_step(f"Upload {self.pipeline} data to S3")
        ## stream 은 학습 데이터만 업로드 함
        local_folder = INPUT_DATA_HOME + "train/"
        print_color(f'[SYSTEM] Start uploading data into S3 from local folder:\n {local_folder}', color='cyan')
        try:
            s3_prefix_uri = "streams/" + f"{stream_id}/{instance_id}/train/data/"
            ### delete & upload data to S3
            self._s3_delete(self.s3_client, self.bucket_name, s3_prefix_uri) 
            for root, dirs, files in os.walk(local_folder):
                for file in enumerate(files):
                    data_path = os.path.join(root, file)
                    if idx == 0: #최초 1회만 delete s3
                        self._s3_update(self.s3_client, self.bucket_name, data_path, local_folder, s3_prefix_uri) 
                    else: 
                        self._s3_update(self.s3_client, self.bucket_name, data_path, local_folder, s3_prefix_uri)
        except Exception as e: 
            raise NotImplementedError(f'[ERROR] Failed to upload local data into S3') 
        ## sol_metadata 업데이트 용 dict 생성
        data_paths = []
        for item in os.listdir(local_folder):
            sub_folder = os.path.join(local_folder, item)
            if os.path.isdir(sub_folder):
                data_paths.append(item+"/")
        if len(data_paths) ==0 :
            uri = {'dataset_uri': ["s3://" + self.bucket_name + "/" + s3_prefix_uri]}
        else:
            uri = {'dataset_uri': []}
            data_path_base = "s3://" + self.bucket_name + "/" +  s3_prefix_uri
            for data_path_sub in data_paths:
                uri['dataset_uri'].append(data_path_base + data_path_sub)

        return uri

    def s3_process(self, bucket_name, data_path, local_folder, s3_path, delete=True):
        if delete == True: 
            objects_to_delete = self.s3_client.list_objects(Bucket=bucket_name, Prefix=s3_path)
            if 'Contents' in objects_to_delete:
                for obj in objects_to_delete['Contents']:
                    self.s3_client.delete_object(Bucket=bucket_name, Key=obj['Key'])
                    print_color(f'[INFO] Deleted pre-existing S3 object:', color = 'yellow')
                    print(f'{obj["Key"]}')
            self.s3_client.delete_object(Bucket=bucket_name, Key=s3_path)
        self.s3_client.put_object(Bucket=bucket_name, Key=(s3_path +'/'))
        try:    
            response = self.s3_client.upload_file(data_path, bucket_name, s3_path + data_path[len(local_folder):])
        except NoCredentialsError as e:
            raise NoCredentialsError(f"[NoCredentialsError] Failed to upload file onto s3: \n {str(e)}")
        except Exception as e:
            raise NotImplementedError(f"Failed to upload file onto s3: \n {str(e)}")
        # temp = s3_path + "/" + data_path[len(local_folder):]
        uploaded_path = bucket_name + '/' + s3_path + data_path[len(local_folder):]
        print_color(f"[SYSTEM] S3 object key (new): ", color='green')
        print(f"{uploaded_path }")
        return 

    def s3_upload_artifacts(self):
        """ 최종 실험결과물 (train & inference) 를 s3 에 업로드 한다. 
        테스트 용으로 활용한다. 
        """
        self.print_step(f"Upload {self.pipeline} artifacts to S3")
        try: 
            s3_prefix_uri = self.set_pipeline_uri(mode="artifact")
        except Exception as e: 
            raise NotImplementedError(f'Failed updating solution_metadata.yaml - << artifact_uri >> info / pipeline: {self.pipeline} \n{e}')
        
        if "train" in self.pipeline:
            artifacts_path = _tar_dir("train_artifacts")  # artifacts tar.gz이 저장된 local 경로 return
            local_folder = os.path.split(artifacts_path)[0] + '/'
            print_color(f'[SYSTEM] Start uploading train artifacts into S3 from local folder:\n {local_folder}', color='cyan')
            self.s3_process(self.bucket_name, artifacts_path, local_folder, s3_prefix_uri) 
            shutil.rmtree(REGISTER_ARTIFACT_PATH , ignore_errors=True)
        elif "inference" in self.pipeline:
            ## inference artifacts tar gz 업로드 
            artifacts_path = _tar_dir("inference_artifacts")  # artifacts tar.gz이 저장된 local 경로 
            local_folder = os.path.split(artifacts_path)[0] + '/'
            print_color(f'[INFO] Start uploading inference artifacts into S3 from local folder:\n {local_folder}', color='cyan')
            self.s3_process(self.bucket_name, artifacts_path, local_folder, s3_prefix_uri)
            shutil.rmtree(REGISTER_ARTIFACT_PATH , ignore_errors=True)
            ## model tar gz 업로드 
            # [중요] model_uri는 inference type 밑에 넣어야되는데, 경로는 inference 대신 train이라고 pipeline 들어가야함 (train artifacts 경로에 저장)
            train_artifacts_s3_path = s3_prefix_uri.replace(f'v{self.solution_version_new}/inference', f'v{self.solution_version_new}/train')
            model_path = _tar_dir("train_artifacts/models")  # model tar.gz이 저장된 local 경로 return 
            local_folder = os.path.split(model_path)[0] + '/'
            print_color(f'\n[SYSTEM] Start uploading << model >> into S3 from local folder: \n {local_folder}', color='cyan')
            # 주의! 이미 train artifacts도 같은 경로에 업로드 했으므로 model.tar.gz올릴 땐 delete object하지 않는다. 
            self.s3_process(self.bucket_name, model_path, local_folder, train_artifacts_s3_path, delete=False) 
            ## model uri 기록
            try: 
                self.set_pipeline_uri(mode="model")
            except Exception as e: 
                raise NotImplementedError(f'[ERROR] Failed updating solution_metadata.yaml - << model_uri >> info / pipeline: {self.pipeline} \n{e}')
            finally:
                shutil.rmtree(REGISTER_MODEL_PATH, ignore_errors=True)
        else:
            raise ValueError(f"Not allowed value for << pipeline >>: {self.pipeline}")

        print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
        self.sol_reg_index = self.sol_reg_index + 1
    ################################
    ######    STEP3. Dcoker Container Control
    ################################

    def make_docker(self, skip_build=False):
        """ECR 에 업로드 할 docker 를 제작 한다. 
        1) experimental_plan 에 사용된 source code 를 임시 폴더로 copy 한다. 
        2) Dockerfile 을 작성 한다. 
        3) Dockerfile 을 컴파일 한다. 
        4) 컴파일 된 도커 파일을 ECR 에 업로드 한다. 
        5) 컨테이너 uri 를 solution_metadata.yaml 에 저장 한다. 
        
        """
        if not skip_build:
            is_docker = (self.infra_setup['BUILD_METHOD'] == 'docker')
            
            builder = "Docker" if is_docker else "Buildah"

            self._reset_alo_solution()  # copy alo folders
            ##TODO : ARM/AMD 에 따라 다른 dockerfile 설정
            self._set_dockerfile()  ## set docerfile

            self.print_step("Set AWS ECR")
            # ecr 상태 확인
            self._set_aws_ecr()
            # if self.infra_setup["BUILD_METHOD"] == 'docker':
                # self._set_aws_ecr(docker=True, tags=self.infra_setup["REPOSITORY_TAGS"])
                # self._set_aws_ecr(docker=False, tags=self.infra_setup["REPOSITORY_TAGS"]) 

            self.print_step(f"setup {builder} Container", sub_title=True)

            self._ecr_login(is_docker=is_docker)

            self.print_step("Create ECR Repository", sub_title=True)

            self._create_ecr_repository(self.infra_setup["REPOSITORY_TAGS"])

            print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
            self.sol_reg_index = self.sol_reg_index + 1

            self.print_step(f"Create {builder} Container", sub_title=True)

            if self.infra_setup['REMOTE_BUILD'] == True: 
                try: 
                    codebuild_client, build_id = self._aws_codebuild() ## remote docker build & ecr push 
                except Exception as e: # FIXME 
                    raise NotImplementedError(str(e))
            else: 
                start = time.time()
                self._build_docker(is_docker=is_docker)
                end = time.time()
                print(f"{builder} build time : {end - start:.5f} sec")
                print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
                self.sol_reg_index = self.sol_reg_index + 1
                
        else:
            if self.infra_setup["BUILD_METHOD"] == 'docker':
                self._set_aws_ecr_skipbuild(docker=True, tags=self.infra_setup["REPOSITORY_TAGS"])
            else:  ##buildah
                self._set_aws_ecr_skipbuild(docker=False, tags=self.infra_setup["REPOSITORY_TAGS"]) 

        # self._set_container_uri()
        if self.infra_setup['REMOTE_BUILD'] == True: 
            return codebuild_client, build_id
        else: 
            return None, None

    def _set_aws_ecr_skipbuild(self, docker = True, tags = []):
        self.docker = docker
        self.ecr_url = self.ecr_name.split("/")[0]
        # FIXME 마지막에 붙는 container 이름은 solution_name 과 같게 
        # http://collab.lge.com/main/pages/viewpage.action?pageId=2126915782
        # [중요] container uri 는 magna-ws 말고 magna 같은 식으로 쓴다 (231207 임현수C)
        ecr_scope = self.infra_setup["WORKSPACE_NAME"].split('-')[0] # magna-ws --> magna
        self.ecr_repo = self.ecr_name.split("/")[1] + '/' + ecr_scope + "/ai-solutions/" + self.solution_name + "/" + self.pipeline + "/"  + self.solution_name  
        self.ecr_full_url = self.ecr_url + '/' + self.ecr_repo 

        print_color(f"[SYSTEM] Target AWS ECR repository:", color='cyan')
        print(f"{self.ecr_repo}")

    def _set_aws_ecr(self):
        self.ecr_url = self.ecr_name.split("/")[0]
        # FIXME 마지막에 붙는 container 이름은 solution_name 과 같게 
        # http://collab.lge.com/main/pages/viewpage.action?pageId=2126915782
        # [중요] container uri 는 magna-ws 말고 magna 같은 식으로 쓴다 (231207 임현수C)
        ecr_scope = self.infra_setup["WORKSPACE_NAME"].split('-')[0] # magna-ws --> magna
        self.ecr_repo = self.ecr_name.split("/")[1] + '/' + ecr_scope + "/ai-solutions/" + self.solution_name + "/" + self.pipeline + "/"  + self.solution_name  
        self.ecr_full_url = self.ecr_url + '/' + self.ecr_repo 
        # get ecr client 
        try: 
            try:
                self.ecr_client = self.session.client('ecr',region_name=self.infra_setup['REGION'])
            except:
                print_color(f"[WARNING] ecr client creation with session failed. Start creating ecr client from boto3", color="yellow")
                self.ecr_client = boto3.client('ecr', region_name=self.infra_setup['REGION'])
        except Exception as e:
            raise ValueError(f"Failed to create ecr client. \n {str(e)}")
        ## 동일 이름의 ECR 존재 시, 삭제하고 다시 생성한다. 
        ## 240324 solution update 시에는 본인 버전만 삭제해야지 통째로 repo 삭제하면 cache 기능 사용불가 
        if self.solution_info['solution_update'] == False:
            try:
                self.ecr_client.delete_repository(repositoryName=self.ecr_repo, force=True)
                print_color(f"[SYSTEM] Repository {self.ecr_repo} already exists. Deleting...", color='yellow')
            except Exception as e:
                print_color(f"[WARNING] Failed to delete pre-existing ECR Repository. \n {str(e)}", color='yellow')
        else: 
            try:
                print_color(f"Now in solution update mode. Only delete current version docker image.", color='yellow')
                resp_ecr_image_list = self.ecr_client.list_images(repositoryName=self.ecr_repo)
                print(resp_ecr_image_list)
                cur_ver_image = []
                for image in resp_ecr_image_list['imageIds']:
                    if 'imageTag' in image.keys():
                        if image['imageTag'] == f'v{self.solution_version_new}':
                            cur_ver_image.append(image)
                # 사실 솔루션 업데이트 시엔 이미 만들어진 현재 버전 이미지가 거의 없을 것임
                if len(cur_ver_image) != 0: 
                    resp_delete_cur_ver = self.ecr_client.batch_delete_image(repositoryName=self.ecr_repo, imageIds=cur_ver_image)
            except Exception as e:
                raise NotImplementedError(f'Failed to delete current versioned image \n {str(e)}') 
        print_color(f"[SYSTEM] target AWS ECR url: \n", color='blue')
        
        print(f"{self.ecr_url}")
        print(self.ecr_full_url)
    

    def buildah_login(self, password):
        login_command = [
            'sudo', 'buildah', 'login',
            '--username', 'AWS',
            '--password-stdin',
            self.ecr_url
        ]
        try:
            p1 = subprocess.Popen(['echo', password], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(login_command, stdin=p1.stdout, stdout=subprocess.PIPE)
            p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits
            output, _ = p2.communicate()
            if p2.returncode != 0:
                raise RuntimeError(output.decode('utf-8'))
            print(f"Successfully logged in to {self.ecr_url} with Buildah")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred during Buildah login: {e.output.decode('utf-8')}")
        except RuntimeError as e:
            print(e)

    def login_to_docker_registry(self, docker_client, username, password, registry):
        try:
            login_response = docker_client.login(
                username=username, 
                password=password, 
                registry=registry
            )

            # 로그인 응답 분석
            if login_response.get('Status') == 'Login Succeeded':
                print('Login succeeded.')
                return True
            else:
                print(f"Login failed: {login_response}")
                # 여기서 로그인 실패에 대한 추가적인 로직을 처리할 수 있습니다.
                return False

        except docker.errors.APIError as e:
            # Docker API 에러 처리
            print(f'An API error occurred: {e}')
            return False
    
    def get_user_password(self):
        
        try:
            ecr_client = self.session.client('ecr', region_name=self.infra_setup['REGION'])
            response = ecr_client.get_authorization_token()
            auth_data = response['authorizationData'][0]
            token = auth_data['authorizationToken']
            import base64
            user, password = base64.b64decode(token).decode('utf-8').split(':')
        except ClientError as e:
            print(f"An error occurred: {e}")
            return None
        
        return user, password

    
    def _ecr_login(self, is_docker):
        
        builder = "Docker" if is_docker else "Buildah"
        
        user, password = self.get_user_password()

        if is_docker:
            self.docker_client = docker.from_env(version='1.24')
            if not self.docker_client.ping():
                raise ValueError("Docker 연결을 실패 했습니다")
            try:
                # ECR에 로그인을 시도합니다. username 대신 'AWS'를 사용합니다.
                login_results = self.docker_client.login(username=user, password=password, registry=self.ecr_url, reauth=True)
                print('login_results {}'.format(login_results))
                print(f"Successfully logged in to {self.ecr_url}")
            except APIError as e:
                print(f"An error occurred during {builder} login: {e}")
        else:
            self.buildah_login(password)

        print_color(f"[SYSTEM] AWS ECR | {builder} login result:", color='cyan')

    def _parse_tags(self, tags):
        """태그 문자열을 파싱하여 딕셔너리 리스트로 변환합니다."""
        parsed_tags = []
        for tag in tags:
            key, value = tag.split(',')
            tag_dict = {
                'Key': key.split('=')[1],
                'Value': value.split('=')[1]
            }
            parsed_tags.append(tag_dict)
        return parsed_tags
    
    def _create_ecr_repository(self, tags):
        if self.solution_info['solution_update'] == False:
            try:
                create_resp = self.ecr_client.create_repository(repositoryName=self.ecr_repo)
                repository_arn = create_resp.get('repository', {}).get('repositoryArn')

                # 태그 파싱
                tags_new = self._parse_tags(tags)

                resp = self.ecr_client.tag_resource(resourceArn=repository_arn, tags=tags_new)

                print_color(f"[SYSTEM] AWS ECR create-repository response: ", color='cyan')
                print(f"{resp}")
            except Exception as e:
                raise NotImplementedError(f"Failed to AWS ECR create-repository:\n + {e}")

        # if len(tags) > 0:
        #     command = [
        #     "aws",
        #     "ecr",
        #     "create-repository",
        #     "--region", self.infra_setup["REGION"],
        #     "--repository-name", self.ecr_repo,
        #     "--image-scanning-configuration", "scanOnPush=true",
        #     "--tags"
        #     ] + tags  # 전달된 태그들을 명령어에 추가합니다.
        # else:
        #     command = [
        #     "aws",
        #     "ecr",
        #     "create-repository",
        #     "--region", self.infra_setup["REGION"],
        #     "--repository-name", self.ecr_repo,
        #     "--image-scanning-configuration", "scanOnPush=true",
        #     ]
        # # create ecr repo 
        # # 240324 solution update True일 땐 이미지만 삭제했으므로 repo를 재생성하진 않는다. 
        # if self.solution_info['solution_update'] == False:
        #     try:
        #         create_resp = self.ecr_client.create_repository(repositoryName=self.ecr_repo)
        #         repository_arn = create_resp['repository']['repositoryArn']
        #         tags_new = []
        #         for tag in tags:
        #                 key, value = tag.split(',')
        #                 tag_dict = {'Key': key.split('=')[1], 'Value': value.split('=')[1]}
        #                 tags_new.append(tag_dict)

        #         resp = self.ecr_client.tag_resource(
        #             resourceArn=repository_arn,
        #             tags=tags_new
        #             )
        #         print_color(f"[SYSTEM] AWS ECR create-repository response: ", color='cyan')
        #         print(f"{resp}")
        #     except Exception as e:
        #         raise NotImplementedError(f"Failed to AWS ECR create-repository:\n + {e}")

    def _aws_codebuild(self):
        # 0. create boto3 session and get codebuild service role arn 
        session = boto3.Session(profile_name=self.infra_setup["AWS_KEY_PROFILE"])
        try: 
            iam_client = session.client('iam', region_name=self.infra_setup["REGION"])
            codebuild_role = iam_client.get_role(RoleName = 'CodeBuildServiceRole')['Role']['Arn']
        except: 
            raise NotImplementedError("Failed to get aws codebuild arn")
        # 1. make buildspec.yml  
        if self.solution_info['inference_arm'] == False:   
            buildspec = self._make_buildspec_commands()
        else: 
            buildspec = self._make_cross_buildspec_commands()
        # 2. make create-codebuild-project.json (trigger: s3)
        s3_prefix_uri = "ai-solutions/" + self.solution_name + \
              f"/v{self.solution_version_new}/" + self.pipeline  + "/codebuild/"
        bucket_uri = self.bucket_name + "/" + s3_prefix_uri 
        codebuild_project_json = self._make_codebuild_s3_project(bucket_uri, codebuild_role)
        # 3. make solution.zip (including buildspec.yml)
        ## .package_list 제외한 나머지 파일,폴더들은 .register_source 폴더로 한번 감싼다. 
        ## .codebuild_solution_zip 폴더 초기화
        if os.path.isdir(AWS_CODEBUILD_ZIP_PATH):
            shutil.rmtree(AWS_CODEBUILD_ZIP_PATH)
        os.makedirs(AWS_CODEBUILD_ZIP_PATH)
        ## docker 내에 필요한 것들 복사
        ## .package_list/{pipe}_pipeline, Dockerfile, solution_metadata.yaml (추론일 때만) 및 buildspec.yml파일은 zip folder 바로 하위에 위치 
        ## Dockerfile 복사 
        shutil.copy2(PROJECT_HOME + "Dockerfile", AWS_CODEBUILD_ZIP_PATH)
        ## solution_metdata.yaml 복사 (추론 일 때만)
        if self.pipeline == 'inference':
            shutil.copy2(SOLUTION_META, AWS_CODEBUILD_ZIP_PATH)
        ## 중요 
        ## REGISTER_SOURCE_PATH --> AWS_CODEBUILD_BUILD_SOURCE_PATH
        shutil.copytree(REGISTER_SOURCE_PATH, AWS_CODEBUILD_BUILD_SOURCE_PATH)
        build_package_path = ASSET_PACKAGE_PATH + f'{self.pipeline}_pipeline'
        shutil.copytree(build_package_path, AWS_CODEBUILD_ZIP_PATH + \
                        ASSET_PACKAGE_DIR + f'{self.pipeline}_pipeline')
        try: # buildspec.yml 파일 save 
            with open(AWS_CODEBUILD_ZIP_PATH + AWS_CODEBUILD_BUILDSPEC_FILE, 'w') as file:
                yaml.safe_dump(buildspec, file)
            print_color(f"[SUCCESS] Saved {AWS_CODEBUILD_BUILDSPEC_FILE} file for aws codebuild", color='green')
        except: 
            raise NotImplementedError(f"Failed to save {AWS_CODEBUILD_BUILDSPEC_FILE} file for aws codebuild")
        # AWS_CODEBUILD_ZIP_PATH --> .zip (AWS_CODEBUILD_S3_SOLUTION_FILE)
        try: 
            shutil.make_archive(PROJECT_HOME + AWS_CODEBUILD_S3_SOLUTION_FILE, 'zip', AWS_CODEBUILD_ZIP_PATH)
            print_color(f"[SUCCESS] Saved {AWS_CODEBUILD_S3_SOLUTION_FILE}.zip file for aws codebuild", color='green')
        except: 
            raise NotImplementedError(f"Failed to save {AWS_CODEBUILD_S3_SOLUTION_FILE}.zip file for aws codebuild")
        # 4. s3 upload solution.zip
        local_file_path = PROJECT_HOME + AWS_CODEBUILD_S3_SOLUTION_FILE + '.zip'
        local_folder = os.path.split(local_file_path)[0] + '/'
        print_color(f'\n[SYSTEM] Start uploading << {AWS_CODEBUILD_S3_SOLUTION_FILE}.zip >> into S3 from local folder:\n {local_folder}', color='cyan')
        self.s3_process(self.bucket_name, local_file_path, local_folder, s3_prefix_uri)
        # 5. run aws codebuild create-project
        try:
            codebuild_client = session.client('codebuild', region_name=self.infra_setup['REGION'])
        except ProfileNotFound:
            print_color(f"[INFO] Start AWS codebuild access check without key file.", color="blue")
            codebuild_client = boto3.client('codebuild', region_name=self.infra_setup['REGION'])
        except Exception as e:
            raise ValueError(f"The credentials are not available: \n {str(e)}")
        # print(codebuild_project_json)
        # 이미 같은 이름의 project 존재하면 삭제 
        #project_name = f'codebuild-project-{self.solution_name}-{self.solution_version_new}'
        ws_name = self.infra_setup["WORKSPACE_NAME"].split('-')[0]
        # project_name에 / 비허용 
        project_name = f'{ws_name}_ai-solutions_{self.solution_name}_v{self.solution_version_new}'
        if project_name in codebuild_client.list_projects()['projects']: 
            resp_delete_proj = codebuild_client.delete_project(name=project_name) 
            print_color(f"[INFO] Deleted pre-existing codebuild project: {project_name} \n {resp_delete_proj}", color='yellow')
        resp_create_proj = codebuild_client.create_project(name = project_name, \
                                                source = codebuild_project_json['source'], \
                                                artifacts = codebuild_project_json['artifacts'], \
                                                cache = codebuild_project_json['cache'], \
                                                tags = codebuild_project_json['tags'], \
                                                environment = codebuild_project_json['environment'], \
                                                logsConfig = codebuild_project_json['logsConfig'], \
                                                serviceRole = codebuild_project_json['serviceRole'])
        # 6. run aws codebuild start-build 
        if type(resp_create_proj)==dict and 'project' in resp_create_proj.keys():
            print_color(f"[SUCCESS] CodeBuild create project response: \n {resp_create_proj}", color='green')
            proj_name = resp_create_proj['project']['name']
            assert type(proj_name) == str
            try: 
                resp_start_build = codebuild_client.start_build(projectName = proj_name)
            except: 
                raise NotImplementedError(f"[FAIL] Failed to start-build CodeBuild project: {proj_name}")
            if type(resp_start_build)==dict and 'build' in resp_start_build.keys(): 
                build_id = resp_start_build['build']['id']
            else: 
                raise ValueError(f"[FAIL] << build id >> not found in response of codebuild - start_build")
        else: 
            raise NotImplementedError(f"[FAIL] Failed to create CodeBuild project \n {resp_create_proj}")           
        return codebuild_client, build_id

    def _make_codebuild_s3_project(self, bucket_uri, codebuild_role):
        with open(AWS_CODEBUILD_S3_PROJECT_FORMAT_FILE) as file:
            codebuild_project_json = json.load(file)
        codebuild_project_json['source']['location'] = bucket_uri + AWS_CODEBUILD_S3_SOLUTION_FILE + '.zip'
        #codebuild_project_json['artifacts']['location'] = self.bucket_name # artifacts location에는 / 비허용
        codebuild_project_json['serviceRole'] = codebuild_role
        codebuild_project_json['environment']['type'] = self.infra_setup["CODEBUILD_ENV_TYPE"]
        codebuild_project_json['environment']['computeType'] = self.infra_setup["CODEBUILD_ENV_COMPUTE_TYPE"]
        codebuild_project_json['environment']['privilegedMode'] = False # True 
        # FIXME tags는 ECR tags와 일단 동일하게 사용 
        def _convert_tags(tags): # inner func.
            if len(tags) > 0:  
                tags_new = []
                for tag in tags:
                        key, value = tag.split(',')
                        tag_dict = {'key': key.split('=')[1], 'value': value.split('=')[1]}
                        tags_new.append(tag_dict)
                return tags_new 
            else: 
                return tags
        # codebuild tags format은 [{'key':'temp-key', 'value':'temp-value'}]
        codebuild_project_json['tags'] = _convert_tags(self.infra_setup["REPOSITORY_TAGS"])
        codebuild_project_json['cache']['location'] = bucket_uri + 'cache'
        codebuild_project_json['logsConfig']['s3Logs']['location'] = bucket_uri + 'logs'
        codebuild_project_json['logsConfig']['s3Logs']['encryptionDisabled'] = True 
        print('codebuild project json: \n', codebuild_project_json)
        return codebuild_project_json
        
    def _make_buildspec_commands(self):
        with open(AWS_CODEBUILD_BUILDSPEC_FORMAT_FILE, 'r') as file: 
            ## {'version': 0.2, 'phases': {'pre_build': {'commands': None}, 'build': {'commands': None}, 'post_build': {'commands': None}}}
            buildspec = yaml.safe_load(file)
        pre_command = [f'aws ecr get-login-password --region {self.infra_setup["REGION"]} | docker login --username AWS --password-stdin {self.ecr_url}/{self.ecr_repo}']
        build_command = ['export DOCKER_BUILDKIT=1']
        if self.solution_info['solution_update'] == True: 
            # 이전 version docker 다운로드 후 이번 version build 시 cache 활용
            pre_command.append(f'docker pull {self.ecr_full_url}:v{self.solution_version_new - 1}')
            build_command.append(f'docker build --build-arg BUILDKIT_INLINE_CACHE=1 --cache-from {self.ecr_full_url}:v{self.solution_version_new - 1} -t {self.ecr_full_url}:v{self.solution_version_new} .')
        else:
            build_command.append(f'docker build --build-arg BUILDKIT_INLINE_CACHE=1 -t {self.ecr_full_url}:v{self.solution_version_new} .')
        post_command = [f'docker push {self.ecr_full_url}:v{self.solution_version_new}']
        buildspec['phases']['pre_build']['commands'] = pre_command
        buildspec['phases']['build']['commands'] = build_command
        buildspec['phases']['post_build']['commands'] = post_command
        del buildspec['phases']['install']
        return buildspec

    def _make_cross_buildspec_commands(self):
        # make buildspec for amd --> arm cross build 
        with open(AWS_CODEBUILD_BUILDSPEC_FORMAT_FILE, 'r') as file: 
            ## {'version': 0.2, 'phases': {'pre_build': {'commands': None}, 'build': {'commands': None}, 'post_build': {'commands': None}}}
            buildspec = yaml.safe_load(file)
        # runtime_docker_version = {'docker': AWS_CODEBUILD_DOCKER_RUNTIME_VERSION} # 19
        install_command = ['docker version', \
                'curl -JLO https://github.com/docker/buildx/releases/download/v0.4.2/buildx-v0.4.2.linux-amd64', \
                'mkdir -p ~/.docker/cli-plugins', \
                'mv buildx-v0.4.2.linux-amd64 ~/.docker/cli-plugins/docker-buildx', \
                'chmod a+rx ~/.docker/cli-plugins/docker-buildx', \
                'docker run --rm tonistiigi/binfmt --install all']
                #'docker run --privileged --rm tonistiigi/binfmt --install all']
        pre_command = [f'aws ecr get-login-password --region {self.infra_setup["REGION"]} | docker login --username AWS --password-stdin {self.ecr_url}/{self.ecr_repo}']
        build_command = ['export DOCKER_BUILDKIT=1', \
                    'docker buildx create --use --name crossx']
        if self.solution_info['solution_update'] == True: 
            # 이전 version docker 다운로드 후 이번 version build 시 cache 활용 
            pre_command.append(f'docker pull {self.ecr_full_url}:v{self.solution_version_new - 1}')
            build_command.append(f'docker buildx build --push --platform=linux/amd64,linux/arm64 --build-arg BUILDKIT_INLINE_CACHE=1 --cache-from {self.ecr_full_url}:v{self.solution_version_new - 1} -t {self.ecr_full_url}:v{self.solution_version_new} .')
        else: 
            build_command.append(f'docker buildx build --push --platform=linux/amd64,linux/arm64 --build-arg BUILDKIT_INLINE_CACHE=1 -t {self.ecr_full_url}:v{self.solution_version_new} .')
        # buildspec['phases']['install']['runtime-versions'] = runtime_docker_version
        buildspec['phases']['install']['commands'] = install_command
        buildspec['phases']['pre_build']['commands'] = pre_command
        buildspec['phases']['build']['commands'] = build_command
        del buildspec['phases']['post_build']
        return buildspec
    
    def _batch_get_builds(self, codebuild_client, build_id, status_period=20):
        # 7. async check remote build status (1check per 10seconds)
        build_status = None 
        while True: 
            resp_batch_get_builds = codebuild_client.batch_get_builds(ids = [build_id])  
            if type(resp_batch_get_builds)==dict and 'builds' in resp_batch_get_builds.keys():
                print_color(f'Response-batch-get-builds: ', color='blue')
                print(resp_batch_get_builds)
                print('-------------------------------------------------------------------------------- \n')
                # assert len(resp_batch_get_builds) == 1 # pipeline 당 build 1회만 할 것이므로 ids 목록엔 1개만 내장
                build_status = resp_batch_get_builds['builds'][0]['buildStatus']
                ## 'SUCCEEDED'|'FAILED'|'FAULT'|'TIMED_OUT'|'IN_PROGRESS'|'STOPPED'
                if build_status == 'SUCCEEDED':
                    print_color(f"[SUCCESS] Completes remote build with AWS CodeBuild", color='green')
                    break 
                elif build_status == 'IN_PROGRESS': 
                    print_color(f"[IN PROGRESS] In progress.. remote building with AWS CodeBuild", color='blue')
                    time.sleep(status_period)
                else: 
                    print_color(f"[FAIL] Failed to remote build with AWS CodeBuild: \n Build Status - {build_status}", color='red')
                    break
        # 8. s3 delete .zip ? 
        return build_status

    def _build_docker(self, is_docker):
        last_update_time = time.time()
        update_interval = 1  # 갱신 주기를 1초로 설정
        log_file_path = f"{self.pipeline}_build.log"
        image_tag = f"{self.ecr_full_url}:v{self.solution_version_new}"
        if is_docker:
            try:
                with open(log_file_path, "w") as log_file:
                    for line in self.docker_client.api.build(path='.', tag=image_tag, decode=True):
                        if 'stream' in line:
                            log_file.write(line['stream'])
                            # 버프에 출력할 내용이 있으면 화면에 출력합니다.
                            if time.time() - last_update_time > update_interval:
                                sys.stdout.write('.')
                                sys.stdout.flush()
                                last_update_time = time.time()
                    sys.stdout.write(' Done!\n')
            except Exception as e:
                print(f"An error occurred: {e}")

        else:
            with open(log_file_path, "wb") as log_file:
                command = ['sudo', 'buildah', 'bud', '--isolation', 'chroot', '-t', image_tag, '.']
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                for line in iter(process.stdout.readline, b''):
                    log_file.write(line)
                    if time.time() - last_update_time > update_interval:
                        sys.stdout.write('.')
                        sys.stdout.flush()
                        last_update_time = time.time()
                process.stdout.close()
                return_code = process.wait()
                if return_code == 0:
                    sys.stdout.write(' Done!\n')
                else:
                    raise ValueError(f"{self.pipeline}_build.log를 확인하세요")

    def docker_push(self):
        image_tag = f"{self.ecr_full_url}:v{self.solution_version_new}"
        self.print_step(f"push {image_tag} Container", sub_title=True)
        if self.infra_setup['BUILD_METHOD'] == 'docker':
            try:
                response = self.docker_client.images.push(image_tag, stream=True, decode=True)
                for line in response:
                    # 진행 중인 작업을 나타내기 위해 '...' 출력
                    sys.stdout.write('.')
                    sys.stdout.flush()
                print("\nDone")
            except Exception as e:
                print(f"Exception occurred: {e}")
        else:
            subprocess.run(['sudo', 'buildah', 'push', f'{self.ecr_full_url}:v{self.solution_version_new}'])
            subprocess.run(['sudo', 'buildah', 'logout', '-a'])

    def _set_container_uri(self):
        try: 
            data = {'container_uri': f'{self.ecr_full_url}:v{self.solution_version_new}'} # full url 는 tag 정보까지 포함 
            self.sm_yaml['pipeline'][self.sm_pipe_pointer].update(data)
            print_color(f"[SYSTEM] Completes setting << container_uri >> in solution_metadata.yaml:", color='green')
            print(f"container_uri: {data['container_uri']}")
            self._save_yaml()
        except Exception as e: 
            raise NotImplementedError(f"Failed to set << container_uri >> in the solution_metadata.yaml \n {str(e)}")

    ################################
    ######    STEP4. Set User Parameters
    ################################

    def set_user_parameters(self, display_table=False):
        """experimental_plan.yaml 에서 제작한 parameter 들으 보여주고, 기능 정의 하도록 한다.
        """
 
        self.print_step(f"Set {self.pipeline} user parameters:")
 
        def rename_key(d, old_key, new_key): #inner func.
            if old_key in d:
                d[new_key] = d.pop(old_key)
       
        ### candidate parameters setting

        params = deepcopy(self.exp_yaml['user_parameters'])
        for pipe_dict in params:
            pipe_name = None # init
            if 'train_pipeline' in list(pipe_dict.keys()):
                pipe_name = 'train'
            elif 'inference_pipeline' in list(pipe_dict.keys()):
                pipe_name = 'inference'
            else:
                pipe_name = None
 
            ## single pipeline 이 지원되도록 하기
            rename_key(pipe_dict, f'{pipe_name}_pipeline', 'candidate_parameters')
            sm_pipe_type = self.sm_yaml['pipeline'][self.sm_pipe_pointer]['type']
 
            if sm_pipe_type == pipe_name:
 
                subkeys = {}
                subkeys.update(pipe_dict)  ## candidate
 
                # 빈 user_parameters 생성
                selected_user_parameters = []
                user_parameters = []
                step_list = []
                for step in pipe_dict['candidate_parameters']:
                    output_data = {'step': step['step'], 'args': {}} # solution metadata v9 기준 args가 dict
                    selected_user_parameters.append(output_data.copy())
                    output_data = {'step': step['step'], 'args': []} # solution metadata v9 기준 args가 list
                    user_parameters.append(output_data.copy())
                    step_list.append(step['step'])
 
                subkeys['selected_user_parameters'] = selected_user_parameters
                subkeys['user_parameters'] = user_parameters
                ## ui 로 표현할 parameter 존재하는지 확인
                try:
                    ui_dict = deepcopy(self.exp_yaml['ui_args_detail'])
                    enable_ui_args = True
                    new_dict = {'user_parameters': {}}
                    for ui_args_step in ui_dict:
                        if f'{pipe_name}_pipeline' in list(ui_args_step.keys()):
                            new_dict['user_parameters'] = ui_args_step[f'{pipe_name}_pipeline']
                except:
                    enable_ui_args = False
 
                ## ui 로 표현할 parameter 존재 시 진행 됨
                if new_dict['user_parameters'] != None: # ui_args_detail의 내용이 None 이면 for 문 에러남 
                    if enable_ui_args:
                        ## step name 추가
                        for new_step in new_dict['user_parameters']:
                            for cnt, steps in enumerate(subkeys['user_parameters']):
                                if steps['step'] == new_step['step']:
                                    subkeys['user_parameters'][cnt]['args'] = new_step['args']

                        ## ui_args_detail 존재 여부 체크
                        print_color("[SYSTEM] Check experimental_plan.yaml - ui_args_detail format", color='green')
                        for candi_step_dict in pipe_dict['candidate_parameters']:
                            if 'ui_args' in candi_step_dict:
                                if candi_step_dict['ui_args']==None or candi_step_dict['ui_args']==[]:
                                    continue
                                for ui_arg in candi_step_dict['ui_args']:
                                    flag = False
                                    ## 존재 여부 검색 시작
                                    ui_dict = deepcopy(self.exp_yaml['ui_args_detail'])
                                    for ui_pipe_dict in ui_dict:
                                        if f'{pipe_name}_pipeline' in list(ui_pipe_dict.keys()):
                                            for ui_step_dict in ui_pipe_dict[f'{pipe_name}_pipeline']:
                                                if candi_step_dict['step'] == ui_step_dict['step']:
                                                    for arg in ui_step_dict['args']:
                                                        if ui_arg == arg['name']:
                                                            flag = True
                                                            print(f"[INFO] Save - ui_args_detail: [{candi_step_dict['step']}]({ui_arg})")
                                    if not flag :
                                        raise ValueError(f"[ERROR] Not found - ui_args_detail: [{candi_step_dict['step']}]({ui_arg})")
       
                self.sm_yaml['pipeline'][self.sm_pipe_pointer].update({'parameters':subkeys})
                # print(subkeys)
                self._save_yaml()

        ## display
        params2 = deepcopy(self.exp_yaml['user_parameters'])
        columns = ['pipeline', 'step', 'parmeter', 'value']
        # df = pd.DataFrame(columns=columns)
        table_idx = 0
        self.candidate_format = {} ## return format 만들기 
        for pipe_dict in params2:
            if 'train_pipeline' in list(pipe_dict.keys()):
                pipe_name = 'train'
                self.candidate_format.update({'train_pipeline':[]})
            elif 'inference_pipeline' in list(pipe_dict.keys()):
                pipe_name = 'inference'
                self.candidate_format.update({'inference_pipeline':[]})
            else:
                pipe_name = None

            step_idx = 0
            item_list = []
            for step_dict in pipe_dict[f'{pipe_name}_pipeline']:
                step_name = step_dict['step']
                new_dict = {'step': step_name, 
                            'args': []}
                self.candidate_format[f'{pipe_name}_pipeline'].append(new_dict)
                try: 
                    for key, value in step_dict['args'][0].items():
                        item = [pipe_name, step_name, key, value]
                        # df.loc[table_idx] = item
                        item_list.append(item)
                        new_dict2 = {
                            'name': key,
                            'description': '',
                            'type': '',
                        }
                        self.candidate_format[f'{pipe_name}_pipeline'][step_idx]['args'].append(new_dict2)
                        table_idx += 1
                except:
                    self.candidate_format[f'{pipe_name}_pipeline'][step_idx]['args'].append({})
                    table_idx += 1
                step_idx += 1
        ## 길이 제한
        # 'text' 컬럼 값을 미리 10글자로 제한
        # MAX_LEN = 40
        # df['value'] = df['value'].astype(str)
        # df['value'] = df['value'].apply(lambda x: x[:MAX_LEN] + '...' if len(x) > MAX_LEN else x)

        if display_table:
            # print_color(df.to_markdown(tablefmt='fancy_grid'), color='cyan')
            print_color(columns, color='cyan')
            for i in item_list: 
                print_color(i, color='cyan')

        print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
        self.sol_reg_index = self.sol_reg_index + 1
        return self.candidate_format
    
    #####################################
    ##### For Debug 
    #####################################
    def register_solution_instance(self): 

        self.print_step("Register AI solution instance")

        if os.path.exists(REGISTER_INTERFACE_PATH + self.SOLUTION_INSTANCE_FILE):
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_INSTANCE_FILE
            print(f'[SYSTEM] AI solution instance 가 등록되어 있어 과정을 생략합니다. (등록정보: {path})')
            return 
        else:
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_FILE
            msg = f"[SYSTEM] AI solution 등록 정보를 {path} 에서 확인합니다."
            load_response = self._load_response_yaml(path, msg)
        print_color('load_response: \n', color='blue')
        print(load_response)
        ########################
        ### instance name      -- spec 변경 시, 수정 필요 (fix date: 24.02.23)
        name = load_response['name'] + \
            "-" + f'v{load_response["versions"][0]["version"]}'
        ########################
        self.solution_instance_params = {
            "workspace_name": load_response['scope_ws']
        }
        print_color(f"\n[INFO] AI solution interface information: \n {self.solution_instance_params}", color='blue')

        # solution_metadata 를 읽어서 json 화 
        with open(SOLUTION_META, 'r') as file:
            yaml_data = yaml.safe_load(file)
        
        self.register_solution_instance_api["name"] = name
        self.register_solution_instance_api["solution_version_id"] = load_response['versions'][0]['id']
        self.register_solution_instance_api["metadata_json"] = yaml_data
        
        if 'train_resource' in self.register_solution_instance_api:
            self.register_solution_instance_api['train_resource'] = "standard"
            yaml_data['pipeline'][0]["resource"]['default'] = self.register_solution_instance_api['train_resource']

        data =json.dumps(self.register_solution_instance_api) # json 화

        # solution instance 등록
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["SOLUTION_INSTANCE"]
        response = requests.post(aic+api, 
                                 params=self.solution_instance_params, 
                                 data=data,
                                 cookies=self.aic_cookie)
        self.response_solution_instance = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] AI solution instance 등록을 성공하였습니다. ", color='cyan')
            print(f"[INFO] response: \n {self.response_solution_instance}")
            print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
            self.sol_reg_index = self.sol_reg_index + 1

            # interface 용 폴더 생성.
            try:
                if not os.path.exists(REGISTER_INTERFACE_PATH):
                    os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory: \n {str(e)}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_INSTANCE_FILE
            with open(path, 'w') as f:
              json.dump(self.response_solution_instance, f, indent=4)
              print_color(f"[SYSTEM] register 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] AI solution instance 등록을 실패하였습니다. 잘못된 요청입니다. ", color='red')
            self.delete_solution()
            raise ValueError("Error message: ", self.response_solution_instance["detail"])

        elif response.status_code == 422:
            print_color("[ERROR] AI solution instance 등록을 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            self.delete_solution()
            raise ValueError("Error message: ", self.response_solution_instance["detail"])
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
            self.delete_solution()
    
    def register_stream(self): 

        self.print_step("Register AI solution stream")

        ## file load 한다. 
        if os.path.exists(REGISTER_INTERFACE_PATH + self.STREAM_FILE):
            path = REGISTER_INTERFACE_PATH + self.STREAM_FILE
            print(f'[SYSTEM] AI solution instance 가 등록되어 있어 과정을 생략합니다. (등록정보: {path})')
            return 
        else:
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_INSTANCE_FILE
            msg = f"[SYSTEM] AI solution instance 등록 정보를 {path} 에서 확인합니다."
            load_response = self._load_response_yaml(path, msg)

        # stream 등록 
        params = {
            "workspace_name": load_response['workspace_name']
        }

        data = {
            "instance_id": load_response['id'],
            "name": load_response['name']  ## prefix name 이 instance 에서 추가 되었으므로 두번 하지 않음
        }
        data =json.dumps(data) # json 화

        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["STREAMS"]
        response = requests.post(aic+api, 
                                 params=params, 
                                 data=data,
                                 cookies=self.aic_cookie)
        self.response_stream = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] Stream 등록을 성공하였습니다. ", color='cyan')
            print(f"[INFO] response: \n {self.response_stream}")

            # interface 용 폴더 생성.
            try:
                if not os.path.exists(REGISTER_INTERFACE_PATH):
                    os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory: \n {str(e)}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.STREAM_FILE
            with open(path, 'w') as f:
              json.dump(self.response_stream, f, indent=4)
              print_color(f"[SYSTEM] register 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] Stream 등록을 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", self.response_stream["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] Stream 등록을 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", self.response_stream["detail"])
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
    
    def request_run_stream(self): 

        self.print_step("Request AI solution stream run")
        ## stream file load 한다. 
        path = REGISTER_INTERFACE_PATH + self.STREAM_FILE
        msg = f"[SYSTEM] Stream 등록 정보를 {path} 에서 확인합니다."
        load_response = self._load_response_yaml(path, msg)

        # stream 등록 
        stream_params = {
            "stream_id": load_response['id'],
            "workspace_name": load_response['workspace_name']
        }
        pprint(stream_params)

        ## v2.3 NEW: stream 이 요구하는 s3 경로에 (edge conductor 처럼) train sample 데이터 업로드 
        dataset_uri = self.s3_upload_stream_data(stream_id=load_response['id'], instance_id=load_response['instance_id'])

        # solution_metadata 를 읽어서 json 화 
        with open(SOLUTION_META, 'r') as file:
            yaml_data = yaml.safe_load(file)
        ##   + dataset_uri 업데이트 하여 등록 함
        for pip in yaml_data['pipeline']:
            if pip['type'] == 'train':
                pip['dataset_uri'] = dataset_uri['dataset_uri']

        data = {
            "metadata_json": yaml_data,
            "config_path": "" # FIXME config_path는 일단 뭐넣을지 몰라서 비워둠 
        }
        data =json.dumps(data) # json 화
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["STREAM_RUN"] + f"/{load_response['id']}"
        response = requests.post(aic+api, params=stream_params, data=data, cookies=self.aic_cookie)
        response_json = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] Stream Run 요청을 성공하였습니다. ", color='cyan')
            print(f"[INFO] response: \n {response_json}")
            print_color(f"alo register step {self.sol_reg_index}/13 complete.", color='PURPLE')
            self.sol_reg_index = self.sol_reg_index + 1

            # interface 용 폴더 생성.
            try:
                if not os.path.exists(REGISTER_INTERFACE_PATH):
                    os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory: \n {str(e)}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.STREAM_RUN_FILE
            with open(path, 'w') as f:
              json.dump(response_json, f, indent=4)
              print_color(f"[SYSTEM] register 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] Stream Run 요청을 실패하였습니다. 잘못된 요청입니다. ", color='red')
            raise ValueError("Error message: {}".format(response_json["detail"]))
        elif response.status_code == 422:
            print_color("[ERROR] Stream Run 요청을 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            raise ValueError("Error message: {}".format(response_json["detail"]))
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
            raise ValueError(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})")

    def get_log_error(self):
        import tarfile
        import io
        def get_log_error(log_file_name, step = ""):
            error_started = False
            log_file = tar.extractfile(log_file_name)
            if log_file:
                for line in log_file:
                    msg = line.decode('utf-8').strip()
                    if "current step" in msg:
                        step = msg.split(":")[1].replace(" ", "")
                    if error_msg in msg:
                        print(f"error step is {step}")
                        error_started = True
                    if error_started:
                        print(msg)
            log_file.close()

        file_name = 'train_artifacts.tar.gz'
        asset_error = "[ASSET]"
        setup_error = "[PROCESS]"
        error_msg = "[ERROR]"
        process_log = 'log/process.log'
        pipeline_log = 'log/pipeline.log'
        step = ""
        s3 = self.session.client('s3')

        # train만 일단 진행하기에 train은 고정
        s3_tar_file_key = "ai-solutions/" + self.solution_name + f"/v{self.solution_version_new}/" + 'train'  + f"/artifacts/{file_name}"

        try:
            s3_object = s3.get_object(Bucket=self.bucket_name, Key=s3_tar_file_key)
            s3_streaming_body = s3_object['Body']

            with io.BytesIO(s3_streaming_body.read()) as tar_gz_stream:
                tar_gz_stream.seek(0)  # 스트림의 시작 지점으로 이동

                with tarfile.open(fileobj=tar_gz_stream, mode='r:gz') as tar:
                    try:
                        get_log_error(process_log, step)
                        get_log_error(pipeline_log, step)
                    except KeyError:
                        print(f'log file is not exist in the tar archive')
        except Exception as e:
            raise NotImplementedError(str(e))
        

    def get_stream_status(self, status_period=10):
        """ KUBEFLOW_STATUS 에서 지원하는 status 별 action 처리를 진행 함. 
            KUBEFLOW_STATUS = ("Pending", "Running", "Succeeded", "Skipped", "Failed", "Error")
            https://www.kubeflow.org/docs/components/pipelines/v2/reference/api/kubeflow-pipeline-api-spec/

          - Pending : docker container 가 실행되기 전 임을 알림.
          - Running : 실행 중 임을 알림.
          - Succeeded : 성공 상태 
          - Skipped : Entity has been skipped. For example, due to caching
          - STOPPED : 중지 상태 
          - FAILED : 실패 상태 

        """
        self.print_step("Get AI solution stream status")
        ## stream file load 한다. 
        path = REGISTER_INTERFACE_PATH + self.STREAM_RUN_FILE
        msg = f"[SYSTEM] Stream 실행 정보를 {path} 에서 확인합니다."
        load_response = self._load_response_yaml(path, msg)

        stream_history_params = {
            "stream_history_id": load_response['id'],
            "workspace_name": load_response['workspace_name']
        }

        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["STREAM_RUN"] + f"/{load_response['id']}/info"

        start_time = time.time()
        time_format = "%Y-%m-%d %H:%M:%S"
        start_time_str = time.strftime(time_format, time.localtime(start_time))
        while True: 
            time.sleep(status_period)

            response = requests.get(aic+api, 
                                    params=stream_history_params, 
                                    cookies=self.aic_cookie)
            self.response_stream_status = response.json()

            if response.status_code == 200:
                # print_color("[SUCCESS] Stream Status 요청을 성공하였습니다. ", color='cyan')
                # print(f"[INFO] response: \n {self.response_stream_status}")

                status = self.response_stream_status["status"]
                status = status.lower()
                if not status in KUBEFLOW_STATUS:
                    raise ValueError(f"[ERROR] 지원하지 않는 status 입니다. (status: {status})")

                end_time = time.time()
                elapsed_time = end_time - start_time
                elapsed_time_str = time.strftime(time_format, time.localtime(elapsed_time))
                ## KUBEFLOW_STATUS = ("Pending", "Running", "Succeeded", "Skipped", "Failed", "Error")
                if status == "succeeded":
                    print_color(f"[SUCCESS] (run_time: {elapsed_time_str}) Train pipeline (status:{status}) 정상적으로 실행 하였습니다. ", color='green')

                    # JSON 데이터를 파일에 저장
                    path = REGISTER_INTERFACE_PATH + self.STREAM_STATUS_FILE
                    with open(path, 'w') as f:
                      json.dump(self.response_stream_status, f, indent=4)
                      print_color(f"[SYSTEM] status 확인 결과를 {path} 에 저장합니다.",  color='green')

                    return status 
                
                elif status == "failed":
                    print_color(f"[ERROR] (start: {start_time_str}, run: {elapsed_time_str}) Train pipeline (status:{status}) 실패 하였습니다. ", color='red')
                    return status 
                elif status == "pending":
                    print_color(f"[INFO] (start: {start_time_str}, run: {elapsed_time_str}) Train pipeline (status:{status}) 준비 중입니다. ", color='yellow')
                    continue
                elif status == "running":
                    print_color(f"[INFO] (start: {start_time_str}, run: {elapsed_time_str}) Train pipeline (status:{status}) 실행 중입니다. ", color='yellow')
                    continue
                elif status == "skipped":
                    print_color(f"[INFO] (start: {start_time_str}, run: {elapsed_time_str}) Train pipeline (status:{status}) 스킵 되었습니다. ", color='yellow')
                    return status 
                elif status == "error":
                    print_color(f"[ERROR] (start: {start_time_str}, run: {elapsed_time_str}) Train pipeline (status:{status}) 에러 발생 하였습니다. ", color='red')
                    return status 
                else:
                    raise ValueError(f"[ERROR] 지원하지 않는 status 입니다. (status: {status})")
                 
            elif response.status_code == 400:
                # print_color("[ERROR] Stream status 요청을 실패하였습니다. 잘못된 요청입니다. ", color='red')
                # print("Error message: ", self.response_stream_status["detail"])
                raise ValueError(f"[ERROR] Stream status 요청을 실패하였습니다. 잘못된 요청입니다. ")
            elif response.status_code == 422:
                print_color("[ERROR] Stream status 요청을 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
                print("Error message: ", self.response_stream_status["detail"])
                raise ValueError(f"[ERROR] Stream status 요청을 실패하였습니다. 유효성 검사를 실패 하였습니다.")
            else:
                print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
                raise ValueError(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})")
    
    def download_artifacts(self): 

        self.print_step("Download train artifacts ")

        def split_s3_path(s3_path): #inner func.
            # 's3://'를 제거하고 '/'를 기준으로 첫 부분을 분리하여 bucket과 나머지 경로를 얻습니다.
            path_parts = s3_path.replace('s3://', '').split('/', 1)
            bucket = path_parts[0]
            rest_of_the_path = path_parts[1]
            return bucket, rest_of_the_path

        try: 
            s3_bucket = split_s3_path(self.stream_history['train_artifact_uri'])[0]
            s3_prefix = split_s3_path(self.stream_history['train_artifact_uri'])[1]
            # S3 버킷에서 파일 목록 가져오기
            objects = self.s3_client.list_objects(Bucket=s3_bucket, Prefix=s3_prefix)
            # 파일 다운로드
            for obj in objects.get('Contents', []):
                key = obj['Key']
                filename = key.split('/')[-1]  # 파일 이름 추출
                self.s3_client.download_file(s3_bucket, key, filename)
                print_color(f'Downloaded: {filename}', color='cyan')
        except: 
            raise NotImplementedError("Failed to download train artifacts.")

    #####################################
    ######    Delete
    #####################################

    def delete_stream_history(self): 
        self.print_step("Delete stream history")

        ## file load 한다. 
        path = REGISTER_INTERFACE_PATH + self.STREAM_RUN_FILE
        msg = f"[SYSTEM] stream 등록 정보를 {path} 에서 확인합니다."
        load_response = self._load_response_yaml(path, msg)

        # stream 등록 
        stream_params = {
            "stream_history_id": load_response['id'],
            "workspace_name": load_response['workspace_name']
        }

        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["STREAMS"] + f"/{load_response['id']}"
        response = requests.delete(aic+api, 
                                 params=stream_params, 
                                 cookies=self.aic_cookie)
        response_delete_stream_history = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] Stream history 삭제를 성공하였습니다. ", color='cyan')
            print(f"[INFO] response: \n {response_delete_stream_history}")

            ## 삭제 성공 시, path 파일 삭제
            if os.path.exists(path):
                os.remove(path)
                print(f'File removed successfully! (file: {path})')
            else:
                print(f'File does not exist! (file: {path})')

        elif response.status_code == 400:
            print_color("[WARNING] Stream history 삭제를 실패하였습니다. 잘못된 요청입니다. ", color='yellow')
            print("Error message: ", response_delete_stream_history["detail"])
            ## 실패하더라도 stream 삭제로 넘어가게 하기
        elif response.status_code == 422:
            print_color("[ERROR] Stream history 삭제를 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", response_delete_stream_history["detail"])
            raise NotImplementedError(f"Failed to delete stream: \n {response_delete_stream_history}")
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
            raise NotImplementedError(f"Failed to delete stream: \n {response_delete_stream_history}")

    def delete_stream(self,solution_id=None): 

        self.print_step("Delete stream")

        if not solution_id:  ## id=none
            ## file load 한다. 
            path = REGISTER_INTERFACE_PATH + self.STREAM_FILE
            msg = f"[SYSTEM] stream 등록 정보를 {path} 에서 확인합니다."
            load_response = self._load_response_yaml(path, msg)

            params = {
                "stream_id": load_response['id'],
                "workspace_name": load_response['workspace_name']
            }
            api = self.api_uri["STREAMS"] + f"/{load_response['id']}"
        else:
            params = {
                "instance_id": solution_id,
                "workspace_name": self.infra_setup['WORKSPACE_NAME']
            }
            api = self.api_uri["STREAMS"] + f"/{solution_id}"
        # stream 등록 

        aic = self.infra_setup["AIC_URI"]
        response = requests.delete(aic+api, 
                                 params=params, 
                                 cookies=self.aic_cookie)
        response_delete_stream = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] Stream 삭제를 성공하였습니다. ", color='cyan')
            print(f"[INFO] response: \n {response_delete_stream}")

            if not solution_id:  ## id=none
                ## 삭제 성공 시, path 파일 삭제
                if os.path.exists(path):
                    os.remove(path)
                    print(f'File removed successfully! (file: {path})')
                else:
                    print(f'File does not exist! (file: {path})')

        elif response.status_code == 400:
            print_color("[ERROR] Stream 삭제를 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", response_delete_stream["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] Stream 삭제를 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", response_delete_stream["detail"])
            raise NotImplementedError(f"Failed to delete stream: \n {response_delete_stream}")
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
            raise NotImplementedError(f"Failed to delete stream: \n {response_delete_stream}")

    def delete_solution_instance(self, solution_id=None): 

        self.print_step("Delete AI solution instance")

        # stream 등록 
        if not solution_id:  ## id=none
            ## file load 한다. 
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_INSTANCE_FILE
            msg = f"[SYSTEM] AI solution instance 등록 정보를 {path} 에서 확인합니다."
            load_response = self._load_response_yaml(path, msg)

            params = {
                "instance_id": load_response['id'],
                "workspace_name": load_response['workspace_name']
            }
            api = self.api_uri["SOLUTION_INSTANCE"] + f"/{load_response['id']}"
        else:
            params = {
                "instance_id": solution_id,
                "workspace_name": self.infra_setup['WORKSPACE_NAME']
            }
            api = self.api_uri["SOLUTION_INSTANCE"] + f"/{solution_id}"

        aic = self.infra_setup["AIC_URI"]
        response = requests.delete(aic+api, 
                                 params=params, 
                                 cookies=self.aic_cookie)
        response_delete_instance = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] AI solution instance 삭제를 성공하였습니다. ", color='cyan')
            print(f"[INFO] response: \n {response_delete_instance}")

            if not solution_id:  ## id=none
                ## 삭제 성공 시, path 파일 삭제
                if os.path.exists(path):
                    os.remove(path)
                    print(f'File removed successfully! (file: {path})')
                else:
                    print(f'File does not exist! (file: {path})')

        elif response.status_code == 400:
            print_color("[ERROR] AI solution insatnce 삭제를 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", response_delete_instance["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] AI solution instance 삭제를 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", response_delete_instance["detail"])
            raise NotImplementedError(f"Failed to delete stream: \n {response_delete_instance}")
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
            raise NotImplementedError(f"Failed to delete stream: \n {response_delete_instance}")

    def delete_solution(self, delete_all=False, solution_id=None): 

        self.print_step("Delete AI solution")
        if self.solution_info["solution_update"]:
            ## file load 한다. 
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_FILE
            msg = f"[SYSTEM] AI solution 등록 정보를 {path} 에서 확인합니다."
            load_response = self._load_response_yaml(path, msg)

            version_id = load_response['versions'][0]['id']
            params = {
                "solution_version_id": version_id,
                "workspace_name": load_response['scope_ws']
            }
            api = self.api_uri["REGISTER_SOLUTION"] + f"/{version_id}/version"
        else:
            if not solution_id:
                ## file load 한다. 
                path = REGISTER_INTERFACE_PATH + self.SOLUTION_FILE
                msg = f"[SYSTEM] AI solution 등록 정보를 {path} 에서 확인합니다."
                load_response = self._load_response_yaml(path, msg)

                params = {
                    "solution_id": load_response['id'],
                    "workspace_name": load_response['scope_ws']
                }
                api = self.api_uri["REGISTER_SOLUTION"] + f"/{load_response['id']}"
            else:
                params = {
                    "solution_id": solution_id,
                    "workspace_name": self.infra_setup["WORKSPACE_NAME"]
                }
                api = self.api_uri["REGISTER_SOLUTION"] + f"/{solution_id}"
        aic = self.infra_setup["AIC_URI"]
        response = requests.delete(aic+api, 
                                 params=params, 
                                 cookies=self.aic_cookie)

        if response.status_code == 200:
            response_delete_solution = response.json()
            print_color("[SUCCESS] AI solution 삭제를 성공하였습니다. ", color='cyan')
            print(f"[INFO] response: \n {response_delete_solution}")

            if not solution_id:  ## id=none
                ## 삭제 성공 시, path 파일 삭제
                if os.path.exists(path):
                    os.remove(path)
                    print(f'File removed successfully! (file: {path})')
                else:
                    print(f'File does not exist! (file: {path})')
        elif response.status_code == 400:
            response_delete_solution = response.json()
            print_color("[ERROR] AI solution 삭제를 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", response_delete_solution["detail"])
        elif response.status_code == 422:
            response_delete_solution = response.json()
            print_color("[ERROR] AI solution 삭제를 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", response_delete_solution["detail"])
            raise NotImplementedError(f"Failed to delete solution: \n {response_delete_solution}")
        elif response.status_code == 500:
            print_color("[ERROR] AI solution 삭제를 실패하였습니다. 잘못된 요청입니다. ", color='red')
        else:
            response_delete_solution = response.json()
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
            raise NotImplementedError(f"Failed to delete solution: \n {response_delete_solution}")

    #####################################
    ######    List Solution & Instance & Stream
    #####################################

    def list_stream(self): 

        self.print_step("List stream ")

        self.stream_params = {
            "workspace_name": self.infra_setup['WORKSPACE_NAME']
        }
        print_color(f"\n[INFO] AI solution interface information: \n {self.stream_params}", color='blue')

        # solution instance 등록
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["STREAMS"]
        response = requests.get(aic+api, 
                                 params=self.stream_params, 
                                 cookies=self.aic_cookie)
        self.stream_list = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] stream list 조회를 성공하였습니다. ", color='cyan')
            pprint("[INFO] response: ")
            for cnt, instance in enumerate(self.stream_list["streams"]):
                id = instance["id"]
                name = instance["name"]

                max_name_len = len(max(name, key=len))
                print(f"(idx: {cnt:{max_name_len}}), stream_name: {name:{max_name_len}}, stream_id: {id}")

            # interface 용 폴더 생성.
            try:
                if not os.path.exists(REGISTER_INTERFACE_PATH):
                    os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory: \n {e}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.STREAM_LIST_FILE
            with open(path, 'w') as f:
              json.dump(self.stream_list, f, indent=4)
              print_color(f"[SYSTEM] list 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] stream list 조회를 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", self.stream_list["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] stream list 조회를 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", self.stream_list["detail"])
            raise NotImplementedError(f"Failed to delete solution: \n {response.status_code}")
        else:
            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')

    def list_stream_history(self, id=''): 

        self.print_step("List stream history")

        self.stream_run_params = {
            "stream_id": id,
            "workspace_name": self.infra_setup['WORKSPACE_NAME']
        }
        print_color(f"\n[INFO] AI solution interface information: \n {self.stream_run_params}", color='blue')

        # solution instance 등록
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["STREAM_RUN"]
        response = requests.get(aic+api, 
                                 params=self.stream_run_params, 
                                 cookies=self.aic_cookie)
        self.stream_history_list = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] AI solution instance 등록을 성공하였습니다. ", color='cyan')
            pprint("[INFO] response: ")
            for cnt, instance in enumerate(self.stream_history_list["stream_histories"]):
                id = instance["id"]
                name = instance["name"]

                max_name_len = len(max(name, key=len))
                print(f"(idx: {cnt:{max_name_len}}), history_name: {name:{max_name_len}}, history_id: {id}")

            # interface 용 폴더 생성.
            try:
                if not os.path.exists(REGISTER_INTERFACE_PATH):
                    os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory: \n {e}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.STREAM_HISTORY_LIST_FILE
            with open(path, 'w') as f:
              json.dump(self.stream_history_list, f, indent=4)
              print_color(f"[SYSTEM] list 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] stream history 조회를 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", self.stream_history_list["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] stream history 조회를 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", self.stream_history_list["detail"])
        else:

            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')

    def list_solution_instance(self): 

        self.print_step("Load AI solution instance list")

        self.solution_instance_params = {
            "workspace_name": self.infra_setup['WORKSPACE_NAME']
        }
        print_color(f"\n[INFO] AI solution interface information: \n {self.solution_instance_params}", color='blue')

        # solution instance 등록
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["SOLUTION_INSTANCE"]
        response = requests.get(aic+api, 
                                 params=self.solution_instance_params, 
                                 cookies=self.aic_cookie)
        self.response_instance_list = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] AI solution instance 등록을 성공하였습니다. ", color='cyan')
            pprint("[INFO] response: ")
            for cnt, instance in enumerate(self.response_instance_list["instances"]):
                id = instance["id"]
                name = instance["name"]

                max_name_len = len(max(name, key=len))
                print(f"(idx: {cnt:{max_name_len}}), instance_name: {name:{max_name_len}}, instance_id: {id}")

            # interface 용 폴더 생성.
            try:
                if not os.path.exists(REGISTER_INTERFACE_PATH):
                    os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory: \n {str(e)}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.INSTANCE_LIST_FILE
            with open(path, 'w') as f:
              json.dump(self.response_instance_list, f, indent=4)
              print_color(f"[SYSTEM] register 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] AI solution instance 등록을 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", self.response_instance_list["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] AI solution instance 등록을 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", self.response_instance_list["detail"])
        else:

            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')

    def list_solution(self): 

        self.print_step("Load AI solution instance list")

        params = {
            "workspace_name": self.infra_setup['WORKSPACE_NAME'],
            "with_pulic": 1, 
            "page_size": 100
        }
        print_color(f"\n[INFO] AI solution interface information: \n {params}", color='blue')

        # solution instance 등록
        aic = self.infra_setup["AIC_URI"]
        api = self.api_uri["SOLUTION_LIST"]
        response = requests.get(aic+api, 
                                 params=params, 
                                 cookies=self.aic_cookie)
        response_json = response.json()

        if response.status_code == 200:
            print_color("[SUCCESS] solution list 조회를 성공하였습니다. ", color='cyan')
            pprint("[INFO] response: ")
            for cnt, instance in enumerate(response_json["solutions"]):
                id = instance["id"]
                name = instance["name"]
                latest_version = instance["versions"][0]["version"]

                max_name_len = len(max(name, key=len))
                print(f"(idx: {cnt:{max_name_len}}), solution_name: {name:{max_name_len}}, solution_id: {id}, latest_version: {latest_version}")

            # interface 용 폴더 생성.
            try:
                if not os.path.exists(REGISTER_INTERFACE_PATH):
                    os.mkdir(REGISTER_INTERFACE_PATH)
            except Exception as e:
                raise NotImplementedError(f"Failed to generate interface directory: \n {str(e)}")

            # JSON 데이터를 파일에 저장
            path = REGISTER_INTERFACE_PATH + self.SOLUTION_LIST_FILE
            with open(path, 'w') as f:
              json.dump(response_json, f, indent=4)
              print_color(f"[SYSTEM] register 결과를 {path} 에 저장합니다.",  color='green')
        elif response.status_code == 400:
            print_color("[ERROR] solution list 조회를 실패하였습니다. 잘못된 요청입니다. ", color='red')
            print("Error message: ", response_json["detail"])
        elif response.status_code == 422:
            print_color("[ERROR] solution list 조회를 실패하였습니다. 유효성 검사를 실패 하였습니다.. ", color='red')
            print("Error message: ", response_json["detail"])
        else:

            print_color(f"[ERROR] 미지원 하는 응답 코드입니다. (code: {response.status_code})", color='red')
    #####################################
    ######    Internal Functions
    #####################################
    def _load_response_yaml(self, path, msg):
        try:
            with open(path) as f:
                data = json.load(f)
                print_color(msg, color='green')
            return data
        except:
            raise ValueError(f"[ERROR] {path} 를 읽기 실패 하였습니다.")
    
    def _init_solution_metadata(self):
        """ Solution Metadata 를 생성합니다. 

        """

        # 각 디렉토리를 반복하며 존재하면 삭제
        for dir_path in [REGISTER_ARTIFACT_PATH, REGISTER_SOURCE_PATH, REGISTER_INTERFACE_PATH]:
            if os.path.isdir(dir_path):
                print(f"Removing directory: {dir_path}")
                shutil.rmtree(dir_path, ignore_errors=False)
                print(f"Directory {dir_path} has been removed successfully.")
            else:
                print(f"Directory {dir_path} does not exist, no action taken.")

        if not type(self.infra_setup['VERSION']) == float:
            raise ValueError("solution_metadata 의 VERSION 은 float 타입이어야 합니다.")

        self.sm_yaml['metadata_version'] = self.infra_setup['VERSION']
        self.sm_yaml['name'] = self.solution_name
        self.sm_yaml['description'] = {}
        self.sm_yaml['pipeline'] = []
        # self.sm_yaml['pipeline'].append({'type': 'inference'})
        try: 
            self._save_yaml()
            if self.debugging:
                print_color(f"\n << solution_metadata.yaml >> generated. - current version: v{self.infra_setup['VERSION']}", color='green')
        except: 
            raise NotImplementedError("Failed to generate << solution_metadata.yaml >>")

    def _sm_append_pipeline(self, pipeline_name): 
        if not pipeline_name in ['train', 'inference']:
            raise ValueError(f"Invalid value ({pipeline_name}). Only one of 'train' or 'inference' is allowed as input.")
        self.sm_yaml['pipeline'].append({'type': pipeline_name})
        self.pipeline = pipeline_name # 가령 inference 파이프라인 추가 시 인스턴스의 pipeline을 inference 로 변경 
        self.sm_pipe_pointer += 1 # 파이프라인 포인터 증가 1
        try: 
            self._save_yaml()
        except: 
            raise NotImplementedError("Failed to update << solution_metadata.yaml >>")
    
    def _save_yaml(self):
        # YAML 파일로 데이터 저장
        class NoAliasDumper(Dumper):
            def ignore_aliases(self, data):
                return True
        with open('solution_metadata.yaml', 'w', encoding='utf-8') as yaml_file:
            yaml.dump(self.sm_yaml, yaml_file, allow_unicode=True, default_flow_style=False, Dumper=NoAliasDumper)
    
    def _set_alo(self):
        self.print_step("Set alo source code for docker container", sub_title=True)
        alo_src = ['main.py', 'src', 'assets', 'solution/experimental_plan.yaml', 'alolib', '.git', 'requirements.txt']
        ## 폴더 초기화
        if os.path.isdir(REGISTER_SOURCE_PATH):
            shutil.rmtree(REGISTER_SOURCE_PATH)
        os.mkdir(REGISTER_SOURCE_PATH)
        ## docker 내에 필요한 것들 복사
        for item in alo_src:
            src_path = PROJECT_HOME + item
            if os.path.isfile(src_path):
                if item == 'solution/experimental_plan.yaml': 
                    register_solution_path = REGISTER_SOURCE_PATH + 'solution/'
                    os.makedirs(register_solution_path , exist_ok=True)
                    shutil.copy2(src_path, register_solution_path)
                    print_color(f'[INFO] copy from " {src_path} "  -->  " {register_solution_path} " ', color='blue')
                else: 
                    shutil.copy2(src_path, REGISTER_SOURCE_PATH)
                    print_color(f'[INFO] copy from " {src_path} "  -->  " {REGISTER_SOURCE_PATH} " ', color='blue')
            elif os.path.isdir(src_path):
                dst_path = REGISTER_SOURCE_PATH  + os.path.basename(src_path)
                shutil.copytree(src_path, dst_path)
                print_color(f'[INFO] copy from " {src_path} "  -->  " {REGISTER_SOURCE_PATH} " ', color='blue')
        
    def _reset_alo_solution(self):
        """ select = all, train, inference 를 지원. experimental 에서 삭제할 사항 선택
        """
        ## v2.3.0 Update: instance 선언 시, self.exp_yaml 화 해두고 있음.(solution 폴더의 오리지널과 다름)
        exp_plan_dict = self.exp_yaml.copy()
     
        for idx, _dict in enumerate(exp_plan_dict['control']):
            if list(map(str, _dict.keys()))[0] == 'get_asset_source':
                if list(map(str, _dict.values()))[0] =='every':
                    exp_plan_dict['control'][idx]['get_asset_source'] = 'once'
            if list(map(str, _dict.keys()))[0] == 'backup_artifacts':
                if list(map(bool, _dict.values()))[0] ==True:
                    exp_plan_dict['control'][idx]['backup_artifacts'] = False
        ## 선택한 사항 삭제
        if self.pipeline == 'train':
            delete_pipeline = 'inference'
        else:
            delete_pipeline = 'train'

        exp_plan_dict['user_parameters'] = [
            item for item in exp_plan_dict['user_parameters']
            if f'{delete_pipeline}_pipeline' not in item
        ]
        exp_plan_dict['asset_source'] = [
            item for item in exp_plan_dict['asset_source']
            if f'{delete_pipeline}_pipeline' not in item
        ]

        ## 다시 저장
        with open(REGISTER_EXPPLAN, 'w') as file:
            yaml.safe_dump(exp_plan_dict, file)

        print_color("[SUCCESS] Success ALO directory setting.", color='green')

    def _set_dockerfile(self):
        try: 
            ## Dockerfile 준비
            if self.pipeline == 'train':
                dockerfile = "TrainDockerfile"
            elif self.pipeline == 'inference':
                dockerfile = "InferenceDockerfile"
            else:
                raise ValueError(f"Invalid value ({self.pipeline}). Only one of 'train' or 'inference' is allowed as input.")
            if os.path.isfile(PROJECT_HOME + dockerfile):
                os.remove(PROJECT_HOME + dockerfile)
            shutil.copy(REGISTER_DOCKER_PATH + dockerfile, PROJECT_HOME)
            os.rename(PROJECT_HOME+dockerfile, PROJECT_HOME + 'Dockerfile')

            docker_location = '/framework/'
            subfolders = []
            # for dirpath, dirnames, filenames in os.walk(ASSET_PACKAGE_PATH):
            #     # dirpath는 현재의 폴더 경로, dirnames는 현재 폴더 아래의 하위 폴더 리스트
            #     for dirname in dirnames:
            #         subfolder_path = os.path.join(dirpath, dirname)  # 하위 폴더의 전체 경로
            #         if self.pipeline not in subfolder_path:
            #             continue
            #         subfolders.append(subfolder_path)
            # step_ 뒤에 붙는 숫자의 크기 기준으로 sort 
            file_list = sorted(next(os.walk(ASSET_PACKAGE_PATH))[2], key=lambda x:int(os.path.splitext(x)[0].split('_')[-1]))
            # train 부터 깔고 inference 깔게끔
            file_list = [i for i in file_list if i.startswith('train')] + [i for i in file_list if i.startswith('inference')]
            search_string = 'site_packages_location'
            with open(PROJECT_HOME + 'Dockerfile', 'r', encoding='utf-8') as file:
                content = file.read()
            # path = subfolders[0].replace(PROJECT_HOME, "")
            path = ASSET_PACKAGE_PATH.replace(PROJECT_HOME, "./")
            replace_string = '\n'.join([f"COPY {path}{file} {docker_location}" for file in file_list])

            requirement_files = [file for file in file_list if file.endswith('.txt')]
            pip_install_commands = '\n'.join([f"RUN pip3 install --no-cache-dir -r {docker_location}{file}" for file in requirement_files])

            if search_string in content:
                content = content.replace(search_string, replace_string + "\n" + pip_install_commands)
                with open(PROJECT_HOME + 'Dockerfile', 'w', encoding='utf-8') as file:
                    file.write(content)

            print_color(f"[SUCESS] set DOCKERFILE for ({self.pipeline}) pipeline", color='green')
        except Exception as e: 
            raise NotImplementedError(f"Failed DOCKERFILE setting. \n - pipeline: {self.pipeline} \n {str(e)}")

    def _check_parammeter(self, param):
        if self._check_str(param):
            return param
        else:
            raise ValueError("You should enter only string value for parameter.")
    def _check_str(self, data):
        return isinstance(data, str)
#----------------------------------------#
#              Common Function           #
#----------------------------------------#
def _tar_dir(_path): 
    ## _path: train_artifacts / inference_artifacts     
    os.makedirs(REGISTER_ARTIFACT_PATH , exist_ok=True)
    os.makedirs(REGISTER_MODEL_PATH, exist_ok=True)
    last_dir = None
    if 'models' in _path: 
        _save_path = REGISTER_MODEL_PATH + 'model.tar.gz'
        last_dir = 'models/'
    else: 
        _save_file_name = _path.strip('.') 
        _save_path = REGISTER_ARTIFACT_PATH +  f'{_save_file_name}.tar.gz' 
        last_dir = _path # ex. train_artifacts/

    tar = tarfile.open(_save_path, 'w:gz')
    for root, dirs, files in os.walk(PROJECT_HOME  + _path):
        base_dir = root.split(last_dir)[-1] + '/'
        for file_name in files:
            tar.add(os.path.join(root, file_name), arcname = base_dir + file_name) # /home부터 시작하는 절대 경로가 아니라 train_artifacts/ 혹은 moddels/부터 시작해서 압축해야하므로 
    tar.close()
    
    return _save_path

def is_float(string):
    try:
        float(string)
        return True 
    except ValueError:
        return False 

def is_int(string):
    try:
        int(string)
        return True 
    except ValueError:
        return False 

# FIXME bool check 어렵 (0이나 1로 입력하면?)
def is_bool(string):
    bool_list = ['True', 'False']
    if string in bool_list: 
        return True 
    else: 
        return False 
    
def is_str(string):
    return isinstance(string, str)

def split_comma(string):
    return [i.strip() for i in string.split(',')]

def convert_string(string_list: list): 
    # string list 내 string들을 float 혹은 int 타입일 경우 해당 타입으로 바꿔줌 
    output_list = [] 
    for string in string_list: 
        if is_int(string): 
            output_list.append(int(string))
        elif is_float(string):
            output_list.append(float(string))
        elif is_bool(string):
            # FIXME 주의: bool(string)이 아니라 eval(string) 으로 해야 정상 작동 
            output_list.append(eval(string)) 
        else: # 무조건 string 
            output_list.append(string)
    return output_list 

def convert_args_type(values: dict):
    '''
    << values smaple >> 
    
    {'name': 'num_hpo',
    'description': 'test3',
    'type': 'int',
    'default': '2',
    'range': '2,5'}
    '''
    output = deepcopy(values) # dict 
    
    arg_type = values['type']
    for k, v in values.items(): 
        if k in ['name', 'description', 'type']: 
            assert type(v) == str 
        elif k == 'selectable': # 전제: selectable은 2개이상 (ex. "1, 2")
            # single 이든 multi 이든 yaml 에 list 형태로 표현  
            assert type(v) == str 
            string_list = split_comma(v)
            assert len(string_list) > 1
            # FIXME 각각의 value들은 type이 제각기 다를 수 있으므로 완벽한 type check는 어려움 
            output[k] = convert_string(string_list) 
        elif k == 'default':
            # 주의: default 는 None이 될수도 있음 (혹은 사용자가 그냥 ""로 입력할 수도 있을듯)
            if (v == None) or (v==""): 
                output[k] = []
                ## FIXME string 일땐 [""] 로 해야하나? 
                if arg_type == 'string': 
                    output[k] = [""] # 주의: EdgeCondcutor UI 에서 null 이 아닌 공백으로 표기 원하면 None 이 아닌 ""로 올려줘야함 
                else: 
                    # FIXME 일단 single(multi)-selection, int, float 일땐 default value가 무조건 있어야 한다고 판단했음 
                    raise ValueError(f"Default value needed for arg. type: << {arg_type} >>")
            else:  
                # FIXME selection 일 때 float, str 같은거 섞여있으면..? 사용자가 1을 의도한건지 '1'을 의도한건지? 
                string_list = split_comma(v)
                if arg_type == 'single_selection': 
                    assert len(string_list) == 1
                elif arg_type == 'multi_selection':
                    assert len(string_list) > 1
                output[k] = convert_string(string_list) # list type     
        elif k == 'range':
            string_list = split_comma(v)
            assert len(string_list) == 2 # range 이므로 [처음, 끝] 2개 
            converted = convert_string(string_list)
            if (arg_type == 'string') or (arg_type == 'int'):
                for i in converted:
                    if not is_int(i): # string type 일 땐 글자 수 range 의미 
                        raise ValueError("<< range >> value must be int")
            elif arg_type == 'float':
                for i in converted:
                    if not is_float(i): # string 글자 수 range 의미 
                        raise ValueError("<< range >> value must be float")
            output[k] = converted 
            
    return output
        