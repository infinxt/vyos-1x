#!/usr/bin/env python3
#
# Copyright (C) 2018 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

import sys
import os
import re
import subprocess
import jinja2
import socket
import time
import syslog as sl

from vyos.config import Config
from vyos import ConfigError

pidfile = r'/var/run/accel_pptp.pid'
pptp_cnf_dir = r'/etc/accel-ppp/pptp'
chap_secrets = pptp_cnf_dir + '/chap-secrets'
pptp_conf = pptp_cnf_dir + '/pptp.config'
# accel-pppd -d -c /etc/accel-ppp/pppoe/pppoe.config -p /var/run/accel_pppoe.pid

### config path creation
if not os.path.exists(pptp_cnf_dir):
  os.makedirs(pptp_cnf_dir)
  sl.syslog(sl.LOG_NOTICE, pptp_cnf_dir  + " created")

pptp_config = '''
### generated by accel_pptp.py ###
[modules]
log_syslog
pptp
ippool
chap-secrets
{% if authentication['auth_proto'] %}
{{ authentication['auth_proto'] }}
{% else %}
auth_mschap_v2
{% endif %}
{% if authentication['mode'] == 'radius' %}
radius
{% endif -%}

[core]
thread-count={{thread_cnt}}

[log]
syslog=accel-pptp,daemon
copy=1
level=5

{% if dns %}
[dns]
{% if dns[0] %}
dns1={{dns[0]}}
{% endif %}
{% if dns[1] %}
dns2={{dns[1]}}
{% endif %}
{% endif %}

{% if wins %}
[wins]
{% if wins[0] %}
wins1={{wins[0]}}
{% endif %}
{% if wins[1] %}
wins2={{wins[1]}}
{% endif %}
{% endif %}

[pptp]
ifname=pptp%d
{% if outside_addr %}
bind={{outside_addr}}
{% endif %}
verbose=1
ppp-max-mtu={{mtu}}
mppe={{authentication['mppe']}}
echo-interval=10
echo-failure=3


[client-ip-range]
0.0.0.0/0

[ip-pool]
tunnel={{client_ip_pool}}
gw-ip-address={{gw_ip}}

{% if authentication['mode'] == 'local' %}
[chap-secrets]
chap-secrets=/etc/accel-ppp/pptp/chap-secrets
{% endif %}

[ppp]
verbose=5
check-ip=1
single-session=replace

{% if authentication['mode'] == 'radius' %}
[radius]
{% for rsrv in authentication['radiussrv']: %}
server={{rsrv}},{{authentication['radiussrv'][rsrv]['secret']}},\
req-limit={{authentication['radiussrv'][rsrv]['req-limit']}},\
fail-time={{authentication['radiussrv'][rsrv]['fail-time']}}
{% endfor %}
timeout=30
acct-timeout=30
max-try=3
{%endif %}

[cli]
tcp=127.0.0.1:2003
'''

### pptp chap secrets
chap_secrets_conf = '''
# username  server  password  acceptable local IP addresses
{% for user in authentication['local-users'] %}
{% if authentication['local-users'][user]['state'] == 'enabled' %}
{{user}}\t*\t{{authentication['local-users'][user]['passwd']}}\t{{authentication['local-users'][user]['ip']}}
{% endif %}
{% endfor %}
'''
###
# inline helper functions
###
# depending on hw and threads, daemon needs a little to start
# if it takes longer than 100 * 0.5 secs, exception is being raised
# not sure if that's the best way to check it, but it worked so far quite well 
###
def chk_con():
  cnt = 0
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  while True:
    try:
      s.connect(("127.0.0.1", 2003))
      break
    except ConnectionRefusedError:
      time.sleep(0.5)
      cnt +=1
      if cnt == 100:
        raise("failed to start pptp server")
        break

### chap_secrets file if auth mode local
def write_chap_secrets(c):
  tmpl = jinja2.Template(chap_secrets_conf, trim_blocks=True)
  chap_secrets_txt = tmpl.render(c)
  old_umask = os.umask(0o077)
  open(chap_secrets,'w').write(chap_secrets_txt)
  os.umask(old_umask)
  sl.syslog(sl.LOG_NOTICE, chap_secrets + ' written')

def accel_cmd(cmd=''):
  if not cmd:
    return None
  try:
    ret = subprocess.check_output(['/usr/bin/accel-cmd','-p','2003',cmd]).decode().strip()
    return ret
  except:
    return 1

### 
# inline helper functions end
###

def get_config():
  c = Config()
  if not c.exists('vpn pptp remote-access '):
    return None

  c.set_level('vpn pptp remote-access')
  config_data = {
    'authentication'  : {
        'mode'          : 'local',
        'local-users'   : {
        },
    'radiussrv'         : {},
    'auth_proto'        : 'auth_mschap_v2',
    'mppe'              : 'require'
    },
    'outside_addr'    : '',
    'dns'             : [],
    'wins'            : [],
    'client_ip_pool'  : '',
    'mtu'   : '1436',
  }

  ### general options ###

  if c.exists('dns-servers server-1'):
    config_data['dns'].append( c.return_value('dns-servers server-1'))
  if c.exists('dns-servers server-2'):
    config_data['dns'].append( c.return_value('dns-servers server-2'))
  if c.exists('wins-servers server-1'):
    config_data['wins'].append( c.return_value('wins-servers server-1'))
  if c.exists('wins-servers server-2'):
    config_data['wins'].append( c.return_value('wins-servers server-2'))
  if c.exists('outside-address'):
    config_data['outside_addr'] = c.return_value('outside-address')

  ### auth local 
  if c.exists('authentication mode local'):
    if c.exists('authentication local-users username'):
      for usr in c.list_nodes('authentication local-users username'):
        config_data['authentication']['local-users'].update(
          {
            usr : {
              'passwd' : '',
              'state'  : 'enabled',
              'ip'     : ''
            }
          }
        )

        if c.exists('authentication local-users username ' + usr + ' password'):
          config_data['authentication']['local-users'][usr]['passwd'] = c.return_value('authentication local-users username ' + usr + ' password')
        if c.exists('authentication local-users username ' + usr + ' disable'):
          config_data['authentication']['local-users'][usr]['state'] = 'disable'
        if c.exists('authentication local-users username ' + usr + ' static-ip'):
          config_data['authentication']['local-users'][usr]['ip'] = c.return_value('authentication local-users username ' + usr + ' static-ip')

  ### authentication mode radius servers and settings

  if c.exists('authentication mode radius'):
    config_data['authentication']['mode'] = 'radius'
    rsrvs = c.list_nodes('authentication radius server')
    for rsrv in rsrvs:
      if c.return_value('authentication radius server ' + rsrv + ' fail-time') == None:
        ftime = '0'
      else:
        ftime = str(c.return_value('authentication radius server ' + rsrv + ' fail-time'))
      if c.return_value('authentication radius-server ' + rsrv + ' req-limit') == None:
        reql = '0'
      else:
        reql = str(c.return_value('authentication radius server ' + rsrv + ' req-limit'))

      config_data['authentication']['radiussrv'].update(
        {
          rsrv  : {
            'secret'  : c.return_value('authentication radius server ' + rsrv + ' key'),
            'fail-time' : ftime,
            'req-limit' : reql
            }
        }
      )

  if c.exists('client-ip-pool'):
    if c.exists('client-ip-pool start'):
      config_data['client_ip_pool'] = c.return_value('client-ip-pool start')
    if c.exists('client-ip-pool stop'):
      config_data['client_ip_pool'] += '-' + re.search('[0-9]+$', c.return_value('client-ip-pool stop')).group(0)
  if c.exists('mtu'):
    config_data['mtu'] = c.return_value('mtu')


  ### gateway address 
  if c.exists('gateway-address'):
    config_data['gw_ip'] = c.return_value('gateway-address') 
  else:
    config_data['gw_ip'] = re.sub('[0-9]+$','1',config_data['client_ip_pool'])    
  
  if c.exists('authentication require'):
    if c.return_value('authentication require') == 'pap':
      config_data['authentication']['auth_proto'] = 'auth_pap'
    if c.return_value('authentication require') == 'chap':
      config_data['authentication']['auth_proto'] = 'auth_chap_md5'
    if c.return_value('authentication require') == 'mschap':
      config_data['authentication']['auth_proto'] = 'auth_mschap_v1'
    if c.return_value('authentication require') == 'mschap-v2':
      config_data['authentication']['auth_proto'] = 'auth_mschap_v2'
  
    if c.exists('authentication mppe'):
      config_data['authentication']['mppe'] = c.return_value('authentication mppe')
  
  return config_data

def verify(c):
  if c == None:
    return None

  if c['authentication']['mode'] == 'local':
    if not c['authentication']['local-users']:
      raise ConfigError('pptp-server authentication local-users required')
    for usr in c['authentication']['local-users']:
      if not c['authentication']['local-users'][usr]['passwd']:
        raise ConfigError('user ' + usr + ' requires a password')

  if c['authentication']['mode'] == 'radius':
    if len(c['authentication']['radiussrv']) == 0:
      raise ConfigError('radius server required')
    for rsrv in c['authentication']['radiussrv']:
      if c['authentication']['radiussrv'][rsrv]['secret'] == None:
        raise ConfigError('radius server ' + rsrv + ' needs a secret configured')

def generate(c):
  if c == None:
    return None
  
  ### accel-cmd reload doesn't work so any change results in a restart of the daemon
  try:
    if os.cpu_count() == 1:
      c['thread_cnt'] = 1
    else:
      c['thread_cnt'] = int(os.cpu_count()/2)
  except KeyError:
    if os.cpu_count() == 1:
      c['thread_cnt'] = 1
    else:
      c['thread_cnt'] = int(os.cpu_count()/2)

  tmpl = jinja2.Template(pptp_config, trim_blocks=True)
  config_text = tmpl.render(c)
  open(pptp_conf,'w').write(config_text)

  if c['authentication']['local-users']:
    write_chap_secrets(c)

  return c

def apply(c):
  if c == None:
    if os.path.exists(pidfile):
      accel_cmd('shutdown hard')
      if os.path.exists(pidfile):
        os.remove(pidfile)
    return None

  if not os.path.exists(pidfile):
    ret = subprocess.call(['/usr/sbin/accel-pppd','-c',pptp_conf,'-p',pidfile,'-d'])
    chk_con()
    if ret !=0 and os.path.exists(pidfile):
      os.remove(pidfile)
      raise ConfigError('accel-pppd failed to start')
  else:
    ### if gw ip changes, only restart doesn't work
    accel_cmd('restart')
    sl.syslog(sl.LOG_NOTICE, "reloading config via daemon restart")

if __name__ == '__main__':
  try:
    c = get_config()
    verify(c)
    generate(c)
    apply(c)
  except ConfigError as e:
    print(e)
    sys.exit(1)
