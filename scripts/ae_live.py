#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,os,signal,subprocess,time
from datetime import datetime,timezone
from pathlib import Path
import paho.mqtt.client as mqtt
ROOT=Path(__file__).resolve().parents[1]; PYTHON=ROOT/'.venv'/'bin'/'python'
def env_load():
 p=ROOT/'.env'
 if p.is_file():
  for line in p.read_text(encoding='utf-8').splitlines():
   line=line.strip()
   if line and not line.startswith('#') and '=' in line:
    k,v=line.split('=',1);os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
def publish(payload):
 try:
  env_load();host=os.getenv('MQTT_HOST','127.0.0.1');port=int(os.getenv('MQTT_PORT','1883'));base=os.getenv('MQTT_BASE_TOPIC','wifi-sensing/v1').rstrip('/')
  try:c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
  except Exception:c=mqtt.Client()
  u=os.getenv('MQTT_SERVER_USER');pw=os.getenv('MQTT_SERVER_PASSWORD')
  if u:c.username_pw_set(u,pw)
  c.connect(host,port,30);c.loop_start();i=c.publish(f'{base}/control/ae-live',json.dumps(payload,separators=(',',':')),qos=1,retain=True);i.wait_for_publish(2);c.loop_stop();c.disconnect()
 except Exception as e:print(f'[WARN] estado live MQTT: {e}')
def main():
 p=argparse.ArgumentParser(description='Inferencia live con AE one-class');p.add_argument('--model',required=True);p.add_argument('--band',default='2.4g',choices=['2.4g','5g']);p.add_argument('--devices',default='rx01,rx02,rx03');args=p.parse_args();model=Path(args.model).expanduser();model=(model if model.is_absolute() else (ROOT/model).resolve());mid=model.name
 receiver_cmd=[str(PYTHON),str(ROOT/'controller'/'csi_receiver.py'),'--ae-model-dir',str(model),'--ae-devices',args.devices];stop=ROOT/'test-logs'/'ae_presence'/'ae_live.stop';stop.parent.mkdir(parents=True,exist_ok=True);stop.unlink(missing_ok=True);controller_cmd=[str(PYTHON),str(ROOT/'controller'/'controller.py'),'--band',args.band,'--devices',args.devices,'--live','--live-stop-file',str(stop)]
 publish({'event':'LIVE_START','model_id':mid,'model':str(model),'band':args.band,'at':datetime.now(timezone.utc).isoformat()});print(f'[AE-LIVE] modelo={model} banda={args.band}; Ctrl+C para detener');receiver=subprocess.Popen(receiver_cmd,cwd=ROOT);controller=None
 try:
  time.sleep(1.2)
  if receiver.poll() is not None:raise RuntimeError('receiver terminó antes de iniciar live')
  controller=subprocess.Popen(controller_cmd,cwd=ROOT)
  while True:
   if controller.poll() is not None:raise RuntimeError(f'controller terminó rc={controller.returncode}')
   if receiver.poll() is not None:raise RuntimeError(f'receiver terminó rc={receiver.returncode}')
   time.sleep(1)
 except KeyboardInterrupt:print('\n[AE-LIVE] deteniendo...')
 finally:
  stop.touch(exist_ok=True)
  for proc in (controller,receiver):
   if proc and proc.poll() is None:proc.send_signal(signal.SIGINT)
  for proc in (controller,receiver):
   if proc:
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.terminate()
  publish({'event':'LIVE_END','model_id':mid,'model':str(model),'band':args.band,'at':datetime.now(timezone.utc).isoformat()})
if __name__=='__main__':main()
