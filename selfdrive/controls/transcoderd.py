#!/usr/bin/env python
import os
import zmq
import time
import json
from tensorflow.python.keras.models import load_model #, Model  #, Sequential
import joblib
import numpy as np
from selfdrive.kegman_conf import kegman_conf
from selfdrive.services import service_list
from enum import Enum
from cereal import log, car
from setproctitle import setproctitle
from common.params import Params 

setproctitle('transcoderd')

#import sys
#sys.stderr = open('../laterald.txt', 'w')

INPUTS = 73
OUTPUTS = 9
MODEL_VERSION = 'F'
MODEL_NAME = 'GRU_Complex_Angle_Mean_Standard_4thOrder_Conv_mae_100_cFactor_2_Advance_0_Lag_15_Smooth_30_Batch_79_6_15_5_Hist_100_Future_0_0_0_Drop_4_Kernel_1_Strides_Prod'
HISTORY_ROWS = 5
OUTPUT_ROWS = 15
BATCH_SIZE = 1
output_standard = joblib.load(os.path.expanduser('models/GRU_Stand_%d_output_%s.scaler' % (OUTPUTS, MODEL_NAME)))
output_scaler = joblib.load(os.path.expanduser('models/GRU_MaxAbs_%d_output_%s.scaler' % (OUTPUTS, MODEL_NAME)))
vehicle_standard = joblib.load(os.path.expanduser('models/GRU_Stand_%d_vehicle_%s.scaler' % (7, MODEL_VERSION)))
vehicle_scaler = joblib.load(os.path.expanduser('models/GRU_MaxAbs_%d_vehicle_%s.scaler' % (7, MODEL_VERSION)))
camera_standard = joblib.load(os.path.expanduser('models/GRU_Stand_%d_camera_%s.scaler' % (32, MODEL_VERSION)))
camera_scaler = joblib.load(os.path.expanduser('models/GRU_MaxAbs_%d_camera_%s.scaler' % (32, MODEL_VERSION)))
model = load_model(os.path.expanduser('models/%s.hdf5' % (MODEL_NAME)))
model_input = np.zeros((BATCH_SIZE,HISTORY_ROWS, INPUTS))
print(model.summary())

def dump_sock(sock, wait_for_one=False):
  if wait_for_one:
    sock.recv()
  while 1:
    try:
      sock.recv(zmq.NOBLOCK)
    except zmq.error.Again:
      break

def pub_sock(port, addr="*"):
  context = zmq.Context.instance()
  sock = context.socket(zmq.PUB)
  sock.bind("tcp://%s:%d" % (addr, port))
  return sock

def sub_sock(port, poller=None, addr="127.0.0.1", conflate=False, timeout=None):
  context = zmq.Context.instance()
  sock = context.socket(zmq.SUB)
  if conflate:
    sock.setsockopt(zmq.CONFLATE, 1)
  sock.connect("tcp://%s:%d" % (addr, port))
  sock.setsockopt(zmq.SUBSCRIBE, b"")

  if timeout is not None:
    sock.RCVTIMEO = timeout

  if poller is not None:
    poller.register(sock, zmq.POLLIN)
  return sock

def tri_blend(l_prob, r_prob, lr_prob, tri_value, minimize=False):
  left = tri_value[:,1:2]
  right = tri_value[:,2:3]
  center = tri_value[:,0:1]
  if minimize:
    abs_left = np.sum(np.absolute(left)) 
    abs_right = np.sum(np.absolute(right))
  else:
    abs_left = 1
    abs_right = 1     
  return [lr_prob * (abs_right * l_prob * left + abs_left * r_prob * right) / (abs_right * l_prob + abs_left * r_prob + 0.0001) + (1-lr_prob) * center, left, right]
  #left_center = l_prob * left + (1-l_prob) * center
  #right_center = r_prob * right + (1-r_prob) * center
  #return [(abs_left * right_center + abs_right * left_center) / (abs_left + abs_right), left, right]
  #return [center, left, right]

def project_error(error):
  error_start = error[0,0]
  error -= error_start
  error_max = np.argmax(error)
  error_min = np.argmin(error)
  if min(error_min, error_max) < 5:
    error[max(error_min, error_max):,0] = error[max(error_min, error_max),0]
  else:
    error[min(error_min, error_max):,0] = error[min(error_min, error_max),0]
  return error_start + error


gernPath = pub_sock(service_list['pathPlan'].port)
gernModelInputs = sub_sock(service_list['model'].port, conflate=True)

frame_count = 1
dashboard_count = 0
lane_width = 0
half_width = 0
width_trim = 0
angle_bias = 0.0
total_offset = 0.0
path_send = log.Event.new_message()
path_send.init('pathPlan')
advanceSteer = 1
one_deg_per_sec = np.ones((OUTPUT_ROWS,1)) / 15
left_center = np.zeros((OUTPUT_ROWS,1))
right_center = np.zeros((OUTPUT_ROWS,1))
calc_center = np.zeros((OUTPUT_ROWS,1))
projected_center = np.zeros((OUTPUT_ROWS,1))
left_probs = np.zeros((OUTPUT_ROWS,1))
right_probs = np.zeros((OUTPUT_ROWS,1))
calc_angles = np.zeros((OUTPUT_ROWS,1))
center_limit = np.reshape(0.5 * np.arange(OUTPUT_ROWS) + 10,(OUTPUT_ROWS,1))
accel_counter = 0   
upper_limit = 0
lower_limit = 0
lr_prob_prev = 0
lr_prob_prev_prev = 0
center_rate_prev = 0
calc_center_prev = calc_center
angle_factor = 1.0
all_inputs = []

execution_time_avg = 0.0
time_factor = 1.0
calibration_factor = 1.0

kegman = kegman_conf()  
all_inputs = [model_input[  :,:,:9],model_input[  :,:,-16:-8], model_input[  :,:,-8:], model_input[  :,:,9:-16]]
new_inputs = [model_input[-1:,:,:9],model_input[-1:,:,-16:-8], model_input[-1:,:,-8:], model_input[-1:,:,9:-16]]

model_output = None
start_time = time.time()

model_output = model.predict(all_inputs)
descaled_output = output_standard.transform(output_scaler.inverse_transform(model_output[-1]))

frame = 0
dump_sock(gernModelInputs, True)
diverging = False

#calibration_items = ['angle_steers','lateral_accelleration','angle_rate_eps', 'yaw_rate_can','far_left_1','far_left_7','far_left_9','far_right_1','far_right_7','far_right_9','left_1','left_7','left_9','right_1','right_7','right_9']
cal_col =           [       1,               2,                    3,                4,            43,         47,          48,           51,           55,           56,        59,      63,      64,       67,       71,       72] 
try:
  with open(os.path.expanduser('~/calibration.json'), 'r') as f:
    calibration = json.load(f)
    calibration = np.array(calibration['calibration'])
    print(calibration)
    calibration_factor = 0.00001
except:
  calibration = np.zeros(len(cal_col))

while 1:
  cs = car.CarState.from_bytes(gernModelInputs.recv())
  start_time = time.time()  
  model_input = np.asarray(cs.modelData).reshape(1, HISTORY_ROWS, INPUTS)
  for i in range(len(cal_col)):
    model_input[:,:,cal_col[i]] -= calibration[i]
  model_input[-1,:,:7] = vehicle_scaler.transform(vehicle_standard.transform(model_input[-1,:,:7]))
  model_input[-1,:,-32:] = camera_scaler.transform(camera_standard.transform(model_input[-1,:,-32:]))
  new_inputs = [model_input[:,:,:9], model_input[:,:,-16:-8], model_input[:,:,-8:], model_input[:,:,9:-16]]
  for i in range(len(all_inputs)):
    all_inputs[i][:-1] = all_inputs[i][1:]
    all_inputs[i][-1:] = new_inputs[i]

  model_output = model.predict_on_batch(all_inputs)

  descaled_output = output_standard.inverse_transform(output_scaler.inverse_transform(model_output[-1])) 
  
  l_prob = min(1, max(0, cs.camLeft.parm4 / 127))
  r_prob = min(1, max(0, cs.camRight.parm4 / 127))
  lr_prob = (l_prob + r_prob) - l_prob * r_prob

  max_width_step = 0.05 * cs.vEgo * l_prob * r_prob
  lane_width = max(570, lane_width - max_width_step * 2, min(1200, lane_width + max_width_step, max(0, cs.camLeft.parm2) - min(0, cs.camRight.parm2)))
  
  calc_angles[:-1] = calc_angles[1:]
  upper_limit = one_deg_per_sec * cs.vEgo * (max(2, min(5, abs(cs.steeringRate))) + accel_counter)
  lower_limit = -upper_limit
  if cs.torqueRequest >= 1:
    upper_limit = one_deg_per_sec * cs.steeringRate
    lower_limit = calc_angles + lower_limit
  elif cs.torqueRequest <= -1:
    lower_limit = one_deg_per_sec * cs.steeringRate
    upper_limit = calc_angles + upper_limit
  else:
    upper_limit = upper_limit + calc_angles
    lower_limit = lower_limit + calc_angles
  if l_prob + r_prob > 0:
    accel_counter = max(0, min(2, accel_counter - 1))
  else:
    accel_counter = max(0, min(2, accel_counter + 1))
  #total_offset = cs.adjustedAngle - cs.steeringAngle
  
  fast_angles = descaled_output[:,0:1]
  slow_angles = descaled_output[:,1:2] 
  fast_angles = advanceSteer * (fast_angles - fast_angles[0]) + cs.steeringAngle + calibration[0] 
  slow_angles = advanceSteer * (slow_angles - slow_angles[0]) + cs.steeringAngle + calibration[0] 
  #calc_angle = np.clip(fast_angles, lower_limit, upper_limit)
  #speed_ratio = min(1, max(0, abs(cs.steeringRate) * 0.2))
  #calc_angle = np.clip((1-speed_ratio) * fast_angles + speed_ratio * slow_angles, lower_limit, upper_limit)
  #calc_angle = slow_angles

  calc_center = tri_blend(l_prob, r_prob, lr_prob, descaled_output[:,2::3], minimize=True)


  '''if cs.vEgo > 10 and l_prob > 0 and r_prob > 0:
    if calc_center[1][0,0] <= calc_center[2][0,0]:
      width_trim -= 1
    else:
      width_trim += 1
  
  if False and abs(cs.steeringRate) < 5 and abs(cs.adjustedAngle) < 3 and cs.torqueRequest != 0 and cs.torqueRequest != 0 and lr_prob > 0 and cs.vEgo > 10:
    if calc_center[0][-1,0] < 0:
      angle_bias -= (0.00001 * cs.vEgo)
    elif calc_center[0][-1,0] > 0:
      angle_bias += (0.00001 * cs.vEgo)'''

  path_send.pathPlan.angleSteers = float(slow_angles[5])
  path_send.pathPlan.mpcAngles = [float(x) for x in slow_angles]
  path_send.pathPlan.slowAngles = [float(x) for x in slow_angles]
  path_send.pathPlan.fastAngles = [float(x) for x in fast_angles]
  path_send.pathPlan.laneWidth = float(lane_width + width_trim)
  path_send.pathPlan.angleOffset = 0  #float(calibration[0])
  path_send.pathPlan.angleBias = angle_bias
  path_send.pathPlan.cPoly = [float(x) for x in (project_error(calc_center[0])[:,0])]
  path_send.pathPlan.lPoly = [float(x) for x in (calc_center[1][:,0] + 0.5 * lane_width)]
  path_send.pathPlan.rPoly = [float(x) for x in (calc_center[2][:,0] - 0.5 * lane_width)]
  path_send.pathPlan.lProb = float(l_prob)
  path_send.pathPlan.rProb = float(r_prob)
  path_send.pathPlan.cProb = float(lr_prob)
  path_send.pathPlan.canTime = cs.canTime
  gernPath.send(path_send.to_bytes())

  frame += 1

  if cs.vEgo > 1 and abs(cs.steeringAngle - calibration[0]) <= 3 and abs(cs.steeringRate) < 5 and lr_prob > 0:
    calibration_factor = max(0.00001, 0.9999 * calibration_factor)
    cal_speed = min(0.1, cs.vEgo * calibration_factor)
    far_left_factor = min(cal_speed, cs.camFarLeft.parm4)
    far_right_factor = min(cal_speed, cs.camFarRight.parm4)
    left_factor = min(cal_speed, cs.camLeft.parm4)
    right_factor = min(cal_speed, cs.camRight.parm4)
    cal_factor = [cal_speed,cal_speed,cal_speed,cal_speed,far_left_factor,far_left_factor,far_left_factor,far_right_factor,far_right_factor,far_right_factor,left_factor,left_factor,left_factor,right_factor,right_factor,right_factor]
    for i in range(len(cal_col)):
      calibration[i] += (cal_factor[i] * (model_input[0][-1:,cal_col[i]] - calibration[i]))
      
  path_send = log.Event.new_message()
  path_send.init('pathPlan')
  if frame % 60 == 0:
    #print(calibration_factor, np.round(calibration, 2))
    print('lane_width: %0.1f center: %0.1f  l_prob:  %0.2f  r_prob:  %0.2f  total_offset:  %0.2f  angle_bias:  %0.2f  model_angle:  %0.2f  model_center_offset:  %0.2f  model exec time:  %0.4fs' % (lane_width, calc_center[0][-1], l_prob, r_prob, total_offset, angle_bias, descaled_output[1,0], descaled_output[1,1], execution_time_avg))

  if frame % 6000 == 0:
    with open(os.path.expanduser('~/calibration.json'), 'w') as f:
      json.dump({'calibration': list(calibration)}, f, indent=2, sort_keys=True)
      os.chmod(os.path.expanduser("~/calibration.json"), 0o764)


  # TODO: replace kegman_conf with params!
  if frame % 100 == 0:
    kegman = kegman_conf()  
    advanceSteer = 1.0 + max(0, float(kegman.conf['advanceSteer']))
    angle_factor = float(kegman.conf['angleFactor'])
    use_bias = float(kegman.conf['angleBias'])
    use_angle_offset = float(kegman.conf['angleOffset'])
    use_lateral_offset = float(kegman.conf['lateralOffset'])

  execution_time_avg += max(0.0001, time_factor) * ((time.time() - start_time) - execution_time_avg)
  time_factor *= 0.96
