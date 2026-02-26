"""orchestrator 데몬 Task Scheduler 등록 스크립트"""
import subprocess
import sys

BAT_PATH = r'C:\Users\rubay\Documents\projects\rion-agent\run_orchestrator_daemon.bat'
TASK_NAME = 'AlohaCTO_Orchestrator_Daemon'

# 기존 작업 삭제
subprocess.run(
    ['schtasks', '/delete', '/tn', TASK_NAME, '/f'],
    capture_output=True
)

# 작업 등록 (로그인 시 실행, 30초 지연)
r = subprocess.run(
    ['schtasks', '/create',
     '/tn', TASK_NAME,
     '/tr', BAT_PATH,
     '/sc', 'ONLOGON',
     '/ru', 'rubay',
     '/f'],
    capture_output=True
)

out = r.stdout.decode('cp949', errors='replace')
err = r.stderr.decode('cp949', errors='replace')
print('STDOUT:', out)
print('STDERR:', err)
print('RC:', r.returncode)

if r.returncode == 0:
    # 등록 후 지연 설정 (XML 수정 방식)
    # 작업 XML 내보내기
    xml_r = subprocess.run(
        ['schtasks', '/query', '/tn', TASK_NAME, '/xml'],
        capture_output=True
    )
    xml_bytes = xml_r.stdout
    xml_text = xml_bytes.decode('utf-16-le', errors='replace').lstrip('\ufeff')

    # DisallowStartIfOnBatteries, ExecutionTimeLimit 수정
    xml_text = xml_text.replace(
        '<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>',
        '<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
    )
    xml_text = xml_text.replace(
        '<StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>',
        '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>'
    )
    # 실행 시간 제한 제거 (무제한)
    if '<ExecutionTimeLimit>' in xml_text:
        import re
        xml_text = re.sub(r'<ExecutionTimeLimit>.*?</ExecutionTimeLimit>', '', xml_text)

    # StartWhenAvailable 추가 (없으면)
    if '<StartWhenAvailable>' not in xml_text:
        xml_text = xml_text.replace(
            '<MultipleInstancesPolicy>',
            '<StartWhenAvailable>true</StartWhenAvailable>\n    <MultipleInstancesPolicy>'
        )

    # RestartOnFailure 추가
    restart_xml = """    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>"""
    if '<RestartOnFailure>' not in xml_text:
        xml_text = xml_text.replace(
            '  </Settings>',
            restart_xml + '\n  </Settings>'
        )

    # 수정된 XML로 재등록
    xml_path = r'C:\Users\rubay\AppData\Local\Temp\aloha_daemon.xml'
    with open(xml_path, 'w', encoding='utf-16') as f:
        f.write(xml_text)

    r2 = subprocess.run(
        ['schtasks', '/delete', '/tn', TASK_NAME, '/f'],
        capture_output=True
    )
    r3 = subprocess.run(
        ['schtasks', '/create', '/tn', TASK_NAME, '/xml', xml_path, '/f'],
        capture_output=True
    )
    print('XML 재등록 RC:', r3.returncode)
    print(r3.stdout.decode('cp949', errors='replace'))
    print(r3.stderr.decode('cp949', errors='replace'))
