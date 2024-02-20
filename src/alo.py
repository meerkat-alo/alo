import os
import sys
import json 
import shutil
import traceback
import subprocess
# Packge
from datetime import datetime
from collections import Counter
from copy import deepcopy
from git import Repo, GitCommandError
# local import
from src.constants import *
from src.artifacts import Aritifacts
from src.install import Packages
from src.pipeline import Pipeline

# 이름을 한번 다시 생각
from src.assets import Assets

from src.external import ExternalHandler 
from src.redisqueue import RedisQueue
from src.logger import ProcessLogger  
# s3를 옮김
from src.sagemaker_handler import SagemakerHandler 
from src.yaml import Metadata
#######################################################################################

class AssetStructure: 
    """Asset 의 In/Out 정보를 저장하는 Data Structure 입니다.

    Attributes:
        self.envs: ALO 가 파이프라인을 실행하는 환경 정보
        self.args: Asset 에서 처리하기 위한 사용자 변수 (experimental_plan 에 정의한 변수를 Asset 내부에서 사용)
            - string, integer, list, dict 타입 지원
        self.data: Asset 에서 사용될 In/Out 데이터 (Tabular 만 지원. 이종의 데이터 포맷은 미지원)
        self.config: Asset 들 사이에서 global 하게 shared 할 설정 값 (Asset 생성자가 추가 가능)
    """
    def __init__(self):
        self.envs = {}
        self.args = {}
        self.data = {} 
        self.config = {}
        
class ALO:
    # def __init__(self, exp_plan_file = None, solution_metadata = None, pipeline_type = 'all', boot_on = False, computing = 'local'):
    def __init__(self, kwargs, experimental_plan = EXP_PLAN, pipeline_type = 'all'):
        """실험 계획 (experimental_plan.yaml), 운영 계획(solution_metadata), 
        파이프라인 종류(train, inference), 동작방식(always-on) 에 대한 설정을 완료함

        Args:
            exp_plan_file: 실험 계획 (experimental_plan.yaml) 을 yaml 파일 위치로 받기
            solution_metadata: 운영 계획 (solution_metadata(str)) 정보를 string 으로 받기
            pipeline_type: 파이프라인 모드 (all, train, inference)
            boot_on: always-on 시, boot 과정 인지 아닌지를  구분 (True, False)
            computing: 학습하는 컴퓨팅 자원 (local, sagemaker)
        Returns:
        """

        # 필요 class init
        self._init_class()
        
        # logger 초기화
        self._init_logger()

        # alolib을 설치
        self._set_alolib()

        self.system_envs = {}

        # TODO default로 EXP PLAN을 넣어 주었는데 아래 if 문과 같이 사용할 되어 지는지 확인***
        if experimental_plan == "" or experimental_plan == None:
            experimental_plan = EXP_PLAN

        # 입력 받은 args를 전역변수로 변환
        # config, system, mode, loop, computing
        for key, value in kwargs.items():
            setattr(self, key, value)

        self._get_alo_version()
        self.set_metadata(experimental_plan, pipeline_type)
        # 현재 ALO 버전

        # solution_metadata 존재 시 self.experimental_plan의 self 변수들 및 system_envs는 _update_yaml에서 업데이트 된다. 
        # self.experimental_plan의 내용 (solution metadata 존재 시 self 변수들 업데이트 완료된)을 ALO 클래스 내부 변수화 
        
        # artifacts home 초기화 (from src.utils)
        self.system_envs['artifacts'] = self.artifact.set_artifacts()

    #############################
    ####    Main Function    ####
    #############################
        
    def main(self):
        """ 실험 계획 (experimental_plan.yaml) 과 운영 계획(solution_metadata) 을 읽어옵니다.
        실험 계획 (experimental_plan.yaml) 은 입력 받은 config 와 동일한 경로에 있어야 합니다.
        운영 계획 (solution_metadata) 은 입력 받은 solution_metadata 값과 동일한 경로에 있어야 합니다.
        """

        pipeline = Pipeline(self.system_envs, self.ext_data)

        # if self.system_envs['pipeline_list'] not in ['train_pipeline', 'inference_pipeline']:
        #     self.proc_logger.process_error(f'Pipeline name in the experimental_plan.yaml \n It must be << train_pipeline >> or << inference_pipeline >>')

        for pipes in self.system_envs['pipeline_list']:
            # TODO 한번에 하려고 하니 이쁘지 않음 논의
            pipeline.setup(pipes, self.experimental_plan)
            pipeline.load(pipes, self.experimental_plan)
            
        # init solution metadata
        if self.loop:
            # boot sequence
            while self.loop:
                # run
                print(self.loop)
        else:
            # run
            print(self.loop)
        
    def set_metadata(self, experimental_plan, pipeline_type):
        """ 실험 계획 (experimental_plan.yaml) 과 운영 계획(solution_metadata) 을 읽어옵니다.
        실험 계획 (experimental_plan.yaml) 은 입력 받은 config 와 동일한 경로에 있어야 합니다.  
        운영 계획 (solution_metadata) 은 입력 받은 solution_metadata 값과 동일한 경로에 있어야 합니다.
        """
        
        # init solution metadata
        sol_meta = self.load_solution_metadata()
        self.system_envs['solution_metadata'] = sol_meta
        self.system_envs['experimental_plan'] = experimental_plan
        self.exp_yaml, sys_envs = self.load_experiment_plan(sol_meta, experimental_plan, self.system_envs)
        self._set_attr()
        # loop 모드면 항상 boot 모드
        self.system_envs = self._set_system_envs(pipeline_type, self.loop, self.system_envs)

        # 입력 받은 config(exp)가 없는 경우 default path에 있는 내용을 사용
        
        # metadata까지 완성되면 출력
        self._alo_info()
        # ALO 설정 완료 info 와 로깅


    def runs(self, mode = None):
        """ 파이프라인 실행에 필요한 데이터, 패키지, Asset 코드를 작업환경으로 setup 하고 & 순차적으로 실행합니다. 

        학습/추론 파이프라인을 순차적으로 실행합니다. (각 한개씩만 지원 multi-pipeline 는 미지원) 
        파이프라인은 외부 데이터 (external_load_data) 로드, Asset 들의 패키지 및 git 설치(setup_asset), Asset 실행(run_asset) 순으로 실행 합니다.


        추론 파이프라인에서는 external_model 의 path 가 존재 시에 load 한다. (학습 파이프라인에서 생성보다 우선순위 높음)

        """

        # summary yaml를 redis q로 put. redis q는 _update_yaml 에서 이미 set 완료  
        # solution meta 존재하면서 (운영 모드) & redis host none아닐때 (edgeapp 모드 > AIC 추론 경우는 아래 코드 미진입) & boot-on이 아닐 때 & inference_pipeline 일 때 save_summary 먼저 반환 필요 
        # Edgeapp과 interface 중인지 (운영 모드인지 체크)

        try: 
            # CHECKLIST preset 과정도 logging 필요하므로 process logger에서는 preset 전에 실행되려면 alolib-source/asset.py에서 log 폴더 생성 필요 (artifacts 폴더 생성전)
            # NOTE 큼직한 단위의 alo.py에서의 로깅은 process logging (인자 X) - train, inference artifacts/log 양쪽에 다 남김 
            if mode != None:
                self.run(mode)
            else:
                for pipeline in self.system_envs["pipeline_list"]:
                    # 입력된 pipeline list 확인
                    self.run(pipeline)
        except: 
            # NOTE [ref] https://medium.com/@rahulkumar_33287/logger-error-versus-logger-exception-4113b39beb4b 
            # NOTE [ref2] https://stackoverflow.com/questions/3702675/catch-and-print-full-python-exception-traceback-without-halting-exiting-the-prog
            # + traceback.format_exc() << 이 방법은 alolib logger에서 exc_info=True 안할 시에 사용가능
            try:  # 여기에 try, finally 구조로 안쓰면 main.py 로 raise 되버리면서 backup_artifacts가 안됨 
                self.proc_logger.process_error("Failed to ALO runs():\n" + traceback.format_exc()) #+ str(e)) 
            finally:
                # 에러 발생 시 self.control['backup_artifacts'] 가 True, False던 상관없이 무조건 backup (폴더명 뒤에 _error 붙여서) 
                # TODO error 발생 시엔 external save 되는 tar.gz도 다른 이름으로 해야할까 ? 
                self.artifact.backup_history(pipeline, self.system_envs['experimental_plan'], self.system_envs['pipeline_start_time'], error=True, size=self.control['backup_size'])
                # error 발생해도 external save artifacts 하도록        
                ext_saved_path = self.ext_data.external_save_artifacts(pipeline, self.external_path, self.external_path_permission)
                if self.is_always_on:
                    fail_str = json.dumps({'status':'fail', 'message':traceback.format_exc()})
                    if self.system_envs['runs_status'] == 'init':
                        self.system_envs['q_inference_summary'].rput(fail_str)
                        self.system_envs['q_inference_artifacts'].rput(fail_str)
                    elif self.system_envs['runs_status'] == 'summary': # 이미 summary는 success로 보낸 상태 
                        self.system_envs['q_inference_artifacts'].rput(fail_str)
        
            
    def sagemaker_runs(self): 
        try:
            try: 
                # FIXME 로컬에서 안돌리면 input 폴더 없으므로 데이터 가져오는 것 여기에 별도 추가 
                self._external_load_data('train_pipeline')
            except Exception as e:
                self.proc_logger.process_error("Failed to get external data. \n" + str(e)) 
                
            try:
                # load sagemaker_config.yaml - (account_id, role, region, ecr_repository, s3_bucket_uri, train_instance_type)
                sm_config = self.experimental_plan.get_yaml(SAGEMAKER_CONFIG) 
                sm_handler = SagemakerHandler(sm_config)
                sm_handler.init()
            except Exception as e:
                self.proc_logger.process_error("Failed to init SagemakerHandler. \n" + str(e)) 
              
            try: 
                sm_handler.setup() 
            except Exception as e: 
                self.proc_logger.process_error(f"Failed to setup SagemakerHandler. \n" + str(e))  
            
            try:
                sm_handler.build_solution()
            except Exception as e: 
                self.proc_logger.process_error(f"Failed to build Sagemaker solution. \n" + str(e))  
                
            try:           
                sm_handler.fit_estimator() 
            except Exception as e: 
                self.proc_logger.process_error(f"Failed to Sagemaker estimator fit. \n" + str(e))  
                
            try: 
                sm_handler.download_latest_model()
            except Exception as e: 
                self.proc_logger.process_error(f"Failed to download sagemaker trained model. \n" + str(e)) 
                
        except:
            self.proc_logger.process_error("Failed to sagemaker runs.") 
            
        finally: 
            # 딱히 안해도 문제는 없는듯 하지만 혹시 모르니 설정했던 환경 변수를 제거 
            os.unsetenv("AWS_PROFILE")

        
    ############################
    ####    Sub Function    ####
    ############################
            
    def run(self, pipeline):
        
        self._set_attr()

        self.system_envs['pipeline_start_time'] = datetime.now().strftime("%y%m%d_%H%M%S")
        # FIXME os env['COMPUTING']은 SagemakerDockerfile에서 설정. sagemaker 일 때만 environment import 
        if os.getenv('COMPUTING') == 'sagemaker':
            from sagemaker_training import environment
            # [중요] sagemaker 사용 시엔 self.external_path['save_train_artifacts_path']를 sagemaker에서 제공하는 model_dir로 변경
            # [참고] https://github.com/aws/sagemaker-training-toolkit        
            self.external_path['save_train_artifacts_path'] = environment.Environment().model_dir
        
        # (self.sol_meta is not None) and 를 어떻게 수정해서 사용할건지 확인
        self.is_always_on = (self.system_envs['redis_host'] is not None) \
            and (self.system_envs['boot_on'] == False) and (pipeline == 'inference_pipeline')
        
        if pipeline not in ['train_pipeline', 'inference_pipeline']:
            self.proc_logger.process_error(f'Pipeline name in the experimental_plan.yaml \n It must be << train_pipeline >> or << inference_pipeline >>')
        ###################################
        ## Step1: artifacts 를 초기화 하기 
        ###################################
        # [주의] 단 .~_artifacts/log 폴더는 지우지 않기! 
        self._empty_artifacts(pipeline)

        ###################################
        ## Step2: 데이터 준비 하기 
        ###################################
        if self.system_envs['boot_on'] == False:  ## boot_on 시, skip
            # NOTE [중요] wrangler_dataset_uri 가 solution_metadata.yaml에 존재했다면,
            # 이미 _update_yaml할 때 exeternal load inference data path로 덮어쓰기 된 상태
            self._external_load_data(pipeline)
        
        # inference pipeline 인 경우, plan yaml의 load_model_path 가 존재 시 .train_artifacts/models/ 를 비우고 외부 경로에서 모델을 새로 가져오기   
        # 왜냐하면 train - inference 둘 다 돌리는 경우도 있기때문 
        # FIXME boot on 때도 모델은 일단 있으면 가져온다 ? 
        if pipeline == 'inference_pipeline':
            try:
                if (self.external_path['load_model_path'] != None) and (self.external_path['load_model_path'] != ""): 
                    self._external_load_model()
            except:
                pass

        # 각 asset import 및 실행 
        try:
            ###################################
            ## Step3: Asset git clone 및 패키지 설치 
            ###################################
            packages = self.setup_asset(pipeline)

            ###################################
            ## Step4: Asset interface 용 data structure 준비 
            ###################################
            self.set_asset_structure()

            ###################################
            ## Step5: Asset 실행 (with asset data structure)  
            ###################################
            self.run_asset(pipeline)
        except: 
            self.proc_logger.process_error(f"Failed to run import: {pipeline}")

        ###################################
        ## Step6: 추론 완료 send summary (운영 추론 모드일 때만 실행)
        ###################################
        
        if self.is_always_on:
            self.system_envs['success_str'] = self.send_summary()

        ###################################
        ## Step7: summary yaml, output 정상 생성 체크    
        ###################################    
        
        #임시
        self.boot_on = False
        if pipeline == 'inference_pipeline' and self.boot_on == False:
            self._check_output()
        
        ###################################
        ## Step8: Artifacts 저장   
        ###################################

        self.save_artifacts(pipeline)

        ###################################
        ## Step9: Artifacts 를 history 에 backup 
        ###################################
        if self.control['backup_artifacts'] == True:
            try:
                self.artifact.backup_history(pipeline, self.system_envs['experimental_plan'], self.system_envs['pipeline_start_time'], size=self.control['backup_size'])
            except: 
                self.proc_logger.process_error("Failed to backup artifacts into << .history >>")

        self.system_envs['proc_finish_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.proc_logger.process_info(f"Process finish-time: {self.system_envs['proc_finish_time']}")


    #####################################
    ####    Part1. Initialization    ####
    #####################################
    def _init_logger(self):
        """ALO Master 의 logger 를 초기화 합니다. 
        ALO Slave (Asset) 의 logger 를 별도 설정 되며, configuration 을 공유 합니다. 
        """

        # 새 runs 시작 시 기존 log 폴더 삭제 
        train_log_path = TRAIN_LOG_PATH
        inference_log_path = INFERENCE_LOG_PATH
        try: 
            if os.path.exists(train_log_path):
                shutil.rmtree(train_log_path, ignore_errors=True)
            if os.path.exists(inference_log_path):
                shutil.rmtree(inference_log_path, ignore_errors=True)
        except: 
            raise NotImplementedError("Failed to empty log directory.")
        # redundant 하더라도 processlogger은 train, inference 양쪽 다남긴다. 
        self.proc_logger = ProcessLogger(PROJECT_HOME)  

    def _set_system_envs(self, pipeline_type, boot_on, _system_envs):
        system_envs = _system_envs
        # 아래 solution metadata 관련 key들은 이미 yaml.py의 _update_yaml에서 setting 돼서 넘어왔으므로, key가 없을때만 None으로 셋팅
        solution_metadata_keys = ['solution_metadata_version', 'q_inference_summary', \
                'q_inference_artifacts', 'q_inference_artifacts', 'redis_host', 'redis_port', \
                'inference_result_datatype', 'train_datatype']
        for k in solution_metadata_keys: 
            if k not in system_envs.keys(): 
                system_envs[k] = None
        if 'pipeline_mode' not in system_envs.keys():
            system_envs['pipeline_mode'] = pipeline_type

        # 'init': initial status / 'summary': success until 'q_inference_summary'/ 'artifacts': success until 'q_inference_artifacts'
        system_envs['runs_status'] = 'init'         
        system_envs['boot_on'] = boot_on
        system_envs['start_time'] = datetime.now().strftime("%y%m%d_%H%M%S")

        if pipeline_type == 'all':
            system_envs['pipeline_list'] = [*self.user_parameters]
        else:
            system_envs['pipeline_list'] = [f"{pipeline_type}_pipeline"]

        # SOLUTION_PIPELINE_MODE 존재 시 (AIC, Sagemaker 등 운영 환경) 해당 pipline만 돌리기가 우선권 
        
        # try:
        #     sol_pipe_mode = os.getenv('SOLUTION_PIPELINE_MODE')
        #     if (sol_pipe_mode is not None) and (sol_pipe_mode not in ['', 'train', 'inference']): 
        #         self.proc_logger.process_error(f"<< SOLUTION_PIPELINE_MODE >> must be << '' >> or << train >> or << inference >>")
        #     if sol_pipe_mode in ['train', 'inference']:
        #         system_envs['pipeline_mode'] = sol_pipe_mode
        #         system_envs['pipeline_list'] = [f"{sol_pipe_mode}_pipeline"]
        #     else:
        #         self.proc_logger.process_info("<< SOLUTION_PIPELINE_MODE >> is now: {sol_pipe_mode} ")
        # except Exception as e:
        #     self.proc_logger.process_error(f"While setting environmental variable << SOLUTION_PIPELINE_MODE >>, error occurs: \n {str(e)}")
            
            
        return system_envs

    def _alo_info(self):
        if self.system_envs['boot_on'] == True: 
            self.proc_logger.process_info(f"==================== Start booting sequence... ====================")
        else: 
            self.proc_logger.process_meta(f"Loaded solution_metadata: \n{self.system_envs['solution_metadata']}\n")
        self.proc_logger.process_info(f"Process start-time: {self.system_envs['start_time']}")
        self.proc_logger.process_meta(f"ALO version = {self.system_envs['alo_version']}")
        self.proc_logger.process_info("==================== Start ALO preset ==================== ")

    def load_solution_metadata(self):
        # TODO solution meta version 관리 필요??
        # system 은 입력받은 solution metadata
        return json.loads(self.system) if self.system != None else None # None or dict from json 
    
    def load_experiment_plan(self, sol_meta, experimental_plan, system_envs):
        return self.experimental_plan.read_yaml(sol_me_file = sol_meta, exp_plan_file = experimental_plan, system_envs = system_envs)

    ###################################
    ####    Part2. Runs fuction    ####
    ###################################
    
    def read_structure(self, pipeline, step):
        import pickle 
        
        a = self.asset_structure.config['meta']['artifacts']['.asset_interface'] + pipeline + "/" + self.user_parameters[pipeline][step]['step'] + "_config.pkl"
        b = self.asset_structure.config['meta']['artifacts']['.asset_interface'] + pipeline + "/" + self.user_parameters[pipeline][step]['step'] + "_data.pkl"

        with open(a, 'rb') as f:
            _config = pickle.load(f)
        
        with open(b, 'rb') as f:
            _data = pickle.load(f)
        return _config, _data
    
    def set_asset_structure(self):
        """Asset 의 In/Out 을 data structure 로 전달한다.
        파이프라인 실행에 필요한 환경 정보를 envs 에 setup 한다.
        """
        self.asset_structure = AssetStructure() 
        
        self.asset_structure.envs['project_home'] = PROJECT_HOME
        
        self.asset_structure.envs['solution_metadata_version'] = self.system_envs['solution_metadata_version']
        self.asset_structure.envs['artifacts'] = self.system_envs['artifacts']
        self.asset_structure.envs['alo_version'] = self.system_envs['alo_version']
        if self.control['interface_mode'] not in INTERFACE_TYPES:
            self.proc_logger.process_error(f"Only << file >> or << memory >> is supported for << interface_mode >>")
        self.asset_structure.envs['interface_mode'] = self.control['interface_mode']
        self.asset_structure.envs['proc_start_time'] = self.system_envs['start_time']
        self.asset_structure.envs['save_train_artifacts_path'] = self.external_path['save_train_artifacts_path']
        self.asset_structure.envs['save_inference_artifacts_path'] = self.external_path['save_inference_artifacts_path']

    
    def setup_asset(self, pipeline):
        """asset 의 git clone 및 패키지를 설치 한다. 
        
        중복된 step 명이 있는지를 검사하고, 존재하면 Error 를 발생한다. 
        always-on 시에는 boot-on 시에만 설치 과정을 진행한다. 

        Args:
          - pipelne(str): train, inference 를 구분한다. 

        Raises:
          - step 명이 동일할 경우 에러 발생 
        """
        # setup asset (asset을 git clone (or local) 및 requirements 설치)
        get_asset_source = self.control["get_asset_source"]  # once, every

        # TODO 현재 pipeline에서 중복된 step 이 있는지 확인
        step_values = [item['step'] for item in self.asset_source[pipeline]]
        step_counts = Counter(step_values)
        for value, count in step_counts.items():
            if count > 1:
                self.proc_logger.process_error(f"Duplicate step exists: {value}")

        # 운영 무한 루프 구조일 땐 boot_on 시 에만 install 하고 이후에는 skip 
        if (self.system_envs['boot_on'] == False) and (self.system_envs['redis_host'] is not None):
            pass 
        else:
            return self._install_steps(pipeline, get_asset_source)
    
    def run_asset(self, pipeline):
        """파이프라인 내의 asset 를 순차적으로 실행한다. 

        Args:
          - pipeline(str) : train, inference 를 구분한다. 

        Raises:
          - Asset 실행 중 에러가 발생할 경우 에러 발생 
          - Asset 실행 중 에러가 발생하지 않았지만 예상하지 못한 에러가 발생할 경우 에러 발생        
        """
        for step, asset_config in enumerate(self.asset_source[pipeline]):    
            self.proc_logger.process_info(f"==================== Start pipeline: {pipeline} / step: {asset_config['step']}")
            # 외부에서 arg를 가져와서 수정이 가능한 구조를 위한 구조
            self.asset_structure.args = self.get_args(pipeline, step)
            try: 
                self.asset_structure = self.process_asset_step(asset_config, step, pipeline, self.asset_structure)
            except: 
                self.proc_logger.process_error(f"Failed to process step: << {asset_config['step']} >>")

    def send_summary(self):
        """save artifacts가 완료되면 OK를 redis q로 put. redis q는 _update_yaml 이미 set 완료  
        solution meta 존재하면서 (운영 모드) &  redis host none아닐때 (edgeapp 모드 > AIC 추론 경우는 아래 코드 미진입) & boot-on이 아닐 때 & inference_pipeline 일 때 save_summary 먼저 반환 필요 
        외부 경로로 잘 artifacts 복사 됐나 체크 (edge app에선 고유한 경로로 항상 줄것임)

        Args:
          - success_str(str): 완료 메시지 
          - ext_saved_path(str): 외부 경로 
        """
        success_str = None
        summary_dir = INFERENCE_SCORE_PATH
        if 'inference_summary.yaml' in os.listdir(summary_dir):
            summary_dict = self.experimental_plan.get_yaml(summary_dir + 'inference_summary.yaml')
            success_str = json.dumps({'status':'success', 'message': summary_dict})
            self.system_envs['q_inference_summary'].rput(success_str)
            self.proc_logger.process_info("Successfully completes putting inference summary into redis queue.")
            self.system_envs['runs_status'] = 'summary'
        else: 
            self.proc_logger.process_error("Failed to redis-put. << inference_summary.yaml >> not found.")
        return success_str

    def save_artifacts(self, pipeline):
        """파이프라인 실행 시 생성된 결과물(artifacts) 를 ./*_artifacts/ 에 저장한다. 
        always-on 모드에서는 redis 로 inference_summary 결과를 Edge App 으로 전송한다. 

        만약, 외부로 결과물 저장 설정이 되있다면, local storage 또는 S3 로 결과값 저장한다. 
        """        
        # s3, nas 등 외부로 artifacts 압축해서 전달 (복사)
        try:      
            ext_saved_path = self.ext_data.external_save_artifacts(pipeline, self.external_path, self.external_path_permission)
        except:
            self.proc_logger.process_error("Failed to save artifacts into external path.") 
        # 운영 추론 모드일 때는 redis로 edgeapp에 artifacts 생성 완료 전달 
        if self.is_always_on:
            if 'inference_artifacts.tar.gz' in os.listdir(ext_saved_path): # 외부 경로 (= edgeapp 단이므로 무조건 로컬경로)
                # send_summary에서 생성된 summary yaml을 다시 한번 전송
                self.system_envs['q_inference_artifacts'].rput(self.system_envs['success_str'])  
                self.proc_logger.process_info("Completes putting artifacts creation << success >> signal into redis queue.")
                self.system_envs['runs_status'] = 'artifacts'
            else: 
                self.proc_logger.process_error("Failed to redis-put. << inference_artifacts.tar.gz >> not found.")

        return ext_saved_path

              
    def _empty_artifacts(self, pipeline): 
        '''
        - pipe_prefix: 'train', 'inference'
        - 주의: log 폴더는 지우지 않기 
        '''
        pipe_prefix = pipeline.split('_')[0]
        dir_artifacts = PROJECT_HOME + f".{pipe_prefix}_artifacts/"
        try: 
            for subdir in os.listdir(dir_artifacts): 
                if subdir == 'log':
                    continue 
                else: 
                    shutil.rmtree(dir_artifacts + subdir, ignore_errors=True)
                    os.makedirs(dir_artifacts + subdir)
                    self.proc_logger.process_info(f"Successfully emptied << {dir_artifacts + subdir} >> ")
        except: 
            self.proc_logger.process_error(f"Failed to empty & re-make << .{pipe_prefix}_artifacts >>")
            

    ########################################
    ####    Part3. Internal fuctions    ####
    ########################################
    
    def _check_output(self):
        """inference_summary.yaml 및 output csv / image 파일 (jpg, png, svg) 정상 생성 체크 
            csv 및 image 파일이 각각 1개씩만 존재해야 한다. 
        Args:
          -
        """   
        # check inference summary 
        if "inference_summary.yaml" in os.listdir(INFERENCE_SCORE_PATH): 
            self.proc_logger.process_info(f"[Success] << inference_summary.yaml >> exists in the inference score path: << {INFERENCE_SCORE_PATH} >>")
        else: 
            self.proc_logger.process_error(f"[Failed] << inference_summary.yaml >> does not exist in the inference score path: << {INFERENCE_SCORE_PATH} >>")
        # check output files  
        output_files = [] 
        for file_path in os.listdir(INFERENCE_OUTPUT_PATH):
        # check if current file_path is a file
            if os.path.isfile(os.path.join(INFERENCE_OUTPUT_PATH, file_path)):
                # add filename to list
                output_files.append(file_path)
        if len(output_files) == 1: 
            if os.path.splitext(output_files[0])[-1] not in TABULAR_OUTPUT_FORMATS + IMAGE_OUTPUT_FORMATS: 
                self.proc_logger.process_error(f"[Failed] output file extension must be one of << {TABULAR_OUTPUT_FORMATS + IMAGE_OUTPUT_FORMATS} >>. \n Your output: {output_files}")
        elif len(output_files) == 2:  
            output_extension = set([os.path.splitext(i)[-1] for i in output_files]) # must be {'.csv', '.jpg' (or other image ext)}
            allowed_extensions = [set(TABULAR_OUTPUT_FORMATS + [i]) for i in IMAGE_OUTPUT_FORMATS]
            if output_extension not in allowed_extensions: 
                self.proc_logger.process_error(f"[Failed] output files extension must be one of << {allowed_extensions} >>. \n Your output: {output_files}") 
        else: 
            self.proc_logger.process_error(f"[Failed] the number of output files must be 1 or 2. \n Your output: {output_files}")
            
    def _external_load_data(self, pipeline):
        """외부 데이터를 가져 옴 (local storage, S3)

        Args:
          - pipelne (str): train / inference 인지를 구분함
        """

        ## from external.py
        self.ext_data.external_load_data(pipeline, self.external_path, self.external_path_permission, self.control['get_external_data'])

    def _external_load_model(self):
        """외부에서 모델파일을 가져옴 (model.tar.gz)

        S3 일 경우 permission 체크를 하고 가져온다.

        """

        ## from external.py
        self.ext_data.external_load_model(self.external_path, self.external_path_permission)
        
    def _install_steps(self, pipeline, get_asset_source='once'):
        requirements_dict = dict() 
        for step, asset_config in enumerate(self.asset_source[pipeline]):
            # self.asset.setup_asset 기능 :
            # local or git pull 결정 및 scripts 폴더 내에 위치시킴 
            self.asset.setup_asset(asset_config, get_asset_source)
            requirements_dict[asset_config['step']] = asset_config['source']['requirements']
        
        return self.install.check_install_requirements(requirements_dict)

    def get_args(self, pipeline, step):
        if type(self.user_parameters[pipeline][step]['args']) == type(None):
            return dict()
        else:
            return self.user_parameters[pipeline][step]['args'][0]

    def process_asset_step(self, asset_config, step, pipeline, asset_structure): 
        # step: int 
        self.asset_structure.envs['pipeline'] = pipeline

        _path = ASSET_HOME + asset_config['step'] + "/"
        _file = "asset_" + asset_config['step']
        # asset2등을 asset으로 수정하는 코드
        _file = ''.join(filter(lambda x: x.isalpha() or x == '_', _file))
        user_asset = self.asset.import_asset(_path, _file)
        if self.system_envs['boot_on'] == True: 
            self.proc_logger.process_info(f"===== Booting... completes importing << {_file} >>")
            return asset_structure

        # 사용자가 config['meta'] 를 통해 볼 수 있는 가변 부
        # FIXME step은 추후 삭제되야함, meta --> metadata 같은 식으로 약어가 아닌 걸로 변경돼야 함 
        meta_dict = {'artifacts': self.system_envs['artifacts'], 'pipeline': pipeline, 'step': step, 'step_number': step, 'step_name': self.user_parameters[pipeline][step]['step']}
        asset_structure.config['meta'] = meta_dict #nested dict

        # TODO 가변부 status는 envs에는 아닌듯 >> 성선임님 논의         
        # asset structure envs pipeline 별 가변부 (alolib에서도 사용하므로 필요)
        if step > 0: 
            asset_structure.envs['prev_step'] = self.user_parameters[pipeline][step - 1]['step'] # asset.py에서 load config, load data 할때 필요 
        asset_structure.envs['step'] = self.user_parameters[pipeline][step]['step']
        asset_structure.envs['num_step'] = step # int  
        asset_structure.envs['asset_branch'] = asset_config['source']['branch']

        ua = user_asset(asset_structure) 
        asset_structure.data, asset_structure.config = ua.run()
     
        # FIXME memory release : on/off 필요 
        try:
            if self.control['reset_assets']:
                self.asset.memory_release(_path)
                sys.path = [item for item in sys.path if asset_structure.envs['step'] not in item]
            else:
                pass
        except:
            self.asset.memory_release(_path)
            sys.path = [item for item in sys.path if asset_structure.envs['step'] not in item]
        
        self.proc_logger.process_info(f"==================== Finish pipeline: {pipeline} / step: {asset_config['step']}")
        
        return asset_structure
    
    def _init_class(self):
        # TODO 지우기 -> Pipeline 클래스에서 사용 예정
        self.ext_data = ExternalHandler()
        self.install = Packages()
        self.asset = Assets(ASSET_HOME)
        self.artifact = Aritifacts()

        self.experimental_plan = Metadata()

    def _set_alolib(self):
        """ALO 는 Master (파이프라인 실행) 와 slave (Asset 실행) 로 구분되어 ALO API 로 통신합니다. 
        기능 업데이트에 따라 API 의 버전 일치를 위해 Master 가 slave 의 버전을 확인하여 최신 버전으로 설치 되도록 강제한다.
        
        """
        # TODO 버전 mis-match 시, git 재설치하기. (미존재시, 에러 발생 시키기)
        try:
            if not os.path.exists(PROJECT_HOME + 'alolib'): 
                ALOMAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                repo = Repo(ALOMAIN)
                ALOVER = repo.active_branch.name
                # repository_url = ALO_LIB_URI
                # destination_directory = ALO_LIB
                cloned_repo = Repo.clone_from(ALO_LIB_URI, ALO_LIB, branch=ALOVER)
                self.proc_logger.process_info(f"alolib {ALOVER} git pull success.")
            else: 
                self.proc_logger.process_info("alolib already exists in local path.")
            alolib_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/alolib/"
            sys.path.append(alolib_path)
        except GitCommandError as e:
            self.proc_logger.process_error(e)
            raise NotImplementedError("alolib git pull failed.")
        req = os.path.join(alolib_path, "requirements.txt")
        # pip package의 안정성이 떨어지기 때문에 subprocess 사용을 권장함
        result = subprocess.run(['pip', 'install', '-r', req], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            self.proc_logger.process_info("Success installing alolib requirements.txt")
            self.proc_logger.process_info(result.stdout)
        else:
            self.proc_logger.process_error(f"Failed installing alolib requirements.txt : \n {result.stderr}")

    def _set_attr(self):
        self.user_parameters = self.experimental_plan.user_parameters
        self.asset_source = self.experimental_plan.asset_source
        self.external_path = self.experimental_plan.external_path
        self.external_path_permission = self.experimental_plan.external_path_permission
        self.control = self.experimental_plan.control

    def _get_alo_version(self):

        with open('.git/HEAD', 'r') as f:
            ref = f.readline().strip()

        # ref는 형식이 'ref: refs/heads/브랜치명' 으로 되어 있으므로, 마지막 부분만 가져옵니다.
        if ref.startswith('ref:'):
            __version__ = ref.split('/')[-1]
        else:
            __version__ = ref  # Detached HEAD 상태 (브랜치명이 아니라 커밋 해시)
        
        self.system_envs['alo_version'] = __version__