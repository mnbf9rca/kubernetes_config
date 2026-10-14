from pathlib import Path
import os, subprocess, tempfile
source = Path(__file__).with_name('hc-mail.sh').read_text()
with tempfile.TemporaryDirectory() as d:
    d=Path(d)
    fallback=d/'sendmail.exim4'
    fallback.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_ARGS"\ncat > "$TEST_MSG"\nexit 42\n')
    fallback.chmod(0o755)
    runner=d/'hc-mail'
    runner.write_text(source.replace('/usr/sbin/sendmail.exim4',str(fallback)))
    runner.chmod(0o755)
    env={**os.environ,'HC_MAIL_DRY':'1','TEST_ARGS':str(d/'args'),'TEST_MSG':str(d/'msg')}
    for subject,suffix in [('SUCCESS',''),('FAILURE','/fail')]:
        p=subprocess.run([str(runner),'-oi','-t'],input=f'To: test-id@hc-ping.com\nSubject: {subject}\n\nreport\n',text=True,capture_output=True,env=env)
        assert p.returncode==0, p.stderr
        assert p.stdout.strip()=='https://hc-ping.com/test-id'+suffix,p.stdout
    msg='To: root\nSubject: system report\n\nkeep this report\n'
    p=subprocess.run([str(runner),'-oi','-t'],input=msg,text=True,capture_output=True,env=env)
    assert p.returncode==42,p.returncode
    assert (d/'args').read_text()=='-oi\n-t\n'
    assert (d/'msg').read_text()==msg
print('PASS: success/failure endpoints; other mail body, arguments and failure status preserved')
