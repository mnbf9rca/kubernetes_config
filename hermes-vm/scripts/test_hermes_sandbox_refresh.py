import json
from pathlib import Path
import subprocess
import tempfile
import unittest

class RefreshTests(unittest.TestCase):
 def run_refresh(self,*,status=None,top='PID CMD\n1 init\n2 sleep\n',fail_pull=False):
  with tempfile.TemporaryDirectory() as scratch:
   root=Path(scratch);(root/'profiles/security_reviewer').mkdir(parents=True)
   source=Path(__file__).with_name('hermes-sandbox-refresh.sh').read_text().replace('HERMES_BIN=/home/hermes/.local/bin/hermes','HERMES_BIN=/usr/bin/true').replace('PROFILE_DIR=$HERMES_HOME/profiles',f'PROFILE_DIR={root}/profiles').replace('main "$@"','')
   (root/'status').write_text(status if isinstance(status,str) else json.dumps(status or dict(gateway_running=True,gateway_state='running',gateway_busy=False,active_agents=0,active_sessions=0)))
   (root/'top').write_text(top)
   mocks='''
terminal_scope() { case "$1" in default) echo 'docker upstream:desktop';; *) echo 'docker reviewer@sha256:abcdef';; esac; }
timeout() { shift; "$@"; }
curl() { cat "$TEST_ROOT/status"; }
docker() {
 case "$1 $2" in
  'pull '*) echo "$2" >> "$TEST_ROOT/pulls"; [ "$FAIL_PULL" = no ];;
  'image inspect') echo new;;
  'ps -a') printf '%s running\\n' "${4#label=hermes-profile=}";;
  'inspect -f') echo old;;
  'top '*) cat "$TEST_ROOT/top";;
  'rm -f') echo "$3" >> "$TEST_ROOT/removed";;
  'image prune') :;;
  *) echo "unexpected Docker call" >&2; return 1;;
 esac
}
main
'''
   script=root/'run.sh';script.write_text(source+'\nTEST_ROOT='+str(root)+'\nFAIL_PULL='+('yes' if fail_pull else 'no')+'\n'+mocks)
   result=subprocess.run(['/bin/sh',str(script)],capture_output=True,text=True)
   return result,(root/'pulls').read_text().splitlines() if (root/'pulls').exists() else [],(root/'removed').read_text().splitlines() if (root/'removed').exists() else []
 def test_two_images_and_actual_scope(self):
  r,pulls,removed=self.run_refresh();self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(pulls,['upstream:desktop','reviewer@sha256:abcdef']);self.assertEqual(set(removed),{'default','security_reviewer'})
  self.assertNotIn('config get',Path(__file__).with_name('hermes-sandbox-refresh.sh').read_text())
 def test_busy_or_unknown_kept(self):
  for status in ('malformed','{}',json.dumps(dict(gateway_running=True,gateway_state='running',gateway_busy=True,active_agents=0,active_sessions=0))):
   r,_,removed=self.run_refresh(status=status);self.assertEqual(removed,[])
  self.assertEqual(self.run_refresh(top='PID CMD\n1 init\n2 sleep\n3 work\n')[2],[])
  # An earlier busy run carries no persisted skip; the next idle run removes it.
  self.assertEqual(len(self.run_refresh()[2]),2)
 def test_pull_failure_summary_redacted(self):
  r,_,removed=self.run_refresh(fail_pull=True);self.assertNotEqual(r.returncode,0);self.assertEqual(removed,[]);self.assertIn('verdict=pull-failed',r.stdout);self.assertNotIn('upstream:',r.stdout);self.assertNotIn('image=',r.stdout)

if __name__=='__main__':unittest.main()
