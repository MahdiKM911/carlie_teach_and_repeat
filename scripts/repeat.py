#!/usr/bin/env python

### IMPORT CLASSES ###
import os
import math
import rospy
import shutil
import cv2 as cv
import numpy as np
import transform_tools
from cv_bridge import CvBridge
from teach_repeat_common import *
from tf.transformations import quaternion_from_euler


### IMPORT MESSAGE TYPES ###
from sensor_msgs.msg import Image
from geometry_msgs.msg import Pose
from nav_msgs.msg import Odometry
from ackermann_msgs.msg import AckermannDriveStamped


### REPEAT NODE CLASS ###
class RepeatNode():

    # INITIALISATION
    def __init__(self):
        # VARIABLES
        self.update_visualisation = False
        self.current_matched_teach_frame_id = 0
        self.odom_topic_recieved = False
        self.frame_counter = 0
        self.frame_id = 0
        self.previous_odom = None # odometry message of previous frame
        self.current_odom = None # odometry message of current frame
        self.first_frame_odom = None # odometry message of first frame

        # ROS INIT NODE
        rospy.init_node('repeat_node')
        rospy.loginfo("Repeat Node Initialised")

        # CONSTANTS
        # image matching constants - you will probably want all of these
        self.PROCESS_EVERY_NTH_FRAME = rospy.get_param('~process_every_nth_frame', 1)
        self.TEACH_DATASET_FILE = rospy.get_param('~teach_dataset', '/home/nvidia/Documents/route_1_processed/dataset.txt')
        self.X_OFFSET_SCALE_FACTOR = rospy.get_param('~x_offset_scale_factor', 0.0)
        self.Y_OFFSET_SCALE_FACTOR = rospy.get_param('~y_offset_scale_factor', 0.02)
        self.YAW_OFFSET_SCALE_FACTOR = rospy.get_param('~yaw_offset_scale_factor', 0.0)

        # image matching constants - you may not need these, feel free to remove/replace
        self.FRAME_SEARCH_WINDOW = rospy.get_param('~frame_search_window', 3)
        self.IMAGE_COMPARISON_SIZE = (rospy.get_param('~image_comparison_size_x', 64), rospy.get_param('~image_comparison_size_y', 48))
        self.PATCH_PORTION = rospy.get_param('~patch_portion', 0.6)
        
        # controller constants
        self.TARGET_FRAME_LOOKAHEAD = max(rospy.get_param('~target_frame_lookahead', 2), 1) # minimum is 1
        self.RHO_GAIN = rospy.get_param('~rho_gain', 0.6)
        self.ALPHA_GAIN = rospy.get_param('~alpha_gain', 0.7)
        self.BETA_GAIN = rospy.get_param('~beta_gain', 0.3)

        self.WHEEL_BASE = rospy.get_param('wheel_base', 0.312)
        self.MAX_FORWARD_VELOCITY = rospy.get_param('max_forward_velocity', 0.5)
        self.MAX_STEERING_ANGLE = rospy.get_param('max_steering_angle', math.radians(45.0))
        self.MIN_STEERING_ANGLE = rospy.get_param('min_steering_angle', math.radians(-45.0))

        # repeat data save constants
        self.SAVE_REPEAT_DATA = rospy.get_param('~save_repeat_dataset', False)
        self.SAVE_IMAGE_RESIZE = (rospy.get_param('~save_image_resize_x', 640), rospy.get_param('~save_image_resize_y', 480))
        self.BASE_PATH = rospy.get_param('~base_path', '/home/nvidia/Documents')
        self.ROUTE_NAME = rospy.get_param('~route_name', 'route_1_processed')

        # other constants
        self.CV_BRIDGE = CvBridge()
        self.VISUALISATION_ON = rospy.get_param('~visualisation_on', True)

        # Setup save directory and dataset file
        if self.SAVE_REPEAT_DATA:
            self.save_path = os.path.join(self.BASE_PATH, self.ROUTE_NAME)
            if os.path.exists(self.save_path):
                shutil.rmtree(self.save_path) # will delete existing save_path directory and its contents
            os.makedirs(self.save_path)
            self.dataset_file = open(os.path.join(self.save_path, 'dataset.txt'), 'w')
            self.dataset_file.write("Frame_ID, relative_odom_x(m), relative_odom_y(m), relative_odom_yaw(rad), relative_pose_x(m), relative_pose_y(m), relative_pose_yaw(rad)\n")

        # Get teach dataset path and read dataset file
        # teach_dataset = [frame_id, relative_odom_x, relative_odom_y, relative_odom_yaw, relative_pose_x, relative_pose_y, relative_pose_yaw]
        if "_processed" in self.TEACH_DATASET_FILE:
            self.teach_dataset_processed_path = os.path.dirname(self.TEACH_DATASET_FILE)
            self.teach_dataset_path = self.teach_dataset_processed_path.replace("_processed", "")
        else:
            self.teach_dataset_processed_path = os.path.dirname(self.TEACH_DATASET_FILE)
            self.teach_dataset_path = os.path.dirname(self.TEACH_DATASET_FILE)

        self.teach_dataset = np.genfromtxt(self.TEACH_DATASET_FILE, delimiter=', ', skip_header=1)
        if len(self.teach_dataset.shape) == 1:
            self.teach_dataset = np.reshape(self.teach_dataset, (1, 7))
        self.teach_dataset[:,0] = np.arange(0, self.teach_dataset.shape[0]) # add in frame IDs to column 1, else will be NAN
        rospy.loginfo('Teach Dataset Size: %d'%(self.teach_dataset.shape[0]))

        # ROS SUBSCRIBERS
        self.odom_subscriber = rospy.Subscriber('odom', Odometry, self.Odom_Callback)
        self.image_subscriber = rospy.Subscriber('image_raw', Image, self.Image_Callback)

        # ROS PUBLISHERS AND MESSAGE SETUP
        self.ackermann_cmd_publisher = rospy.Publisher('/carlie/ackermann_cmd/autonomous', AckermannDriveStamped, queue_size=10)

		# Setup ROS Ackermann Drive Command Message
        self.ackermann_cmd = AckermannDriveStamped()
        self.ackermann_cmd.drive.steering_angle_velocity = 0.0 # see AckermannDriveStamped message for definition
        self.ackermann_cmd.drive.acceleration = rospy.get_param('acceleration', 0.5) # see AckermannDriveStamped message for definition
        self.ackermann_cmd.drive.jerk = 0 # see AckermannDriveStamped message for definition

        # CREATE OPENCV WINDOWS
        if self.VISUALISATION_ON:
            cv.namedWindow('Repeat Image', cv.WINDOW_NORMAL)
            cv.namedWindow('Matched Teach Image', cv.WINDOW_NORMAL)

        # ROS SPIN
        while not rospy.is_shutdown():
            if self.update_visualisation and self.VISUALISATION_ON:
                teach_img = cv.imread(os.path.join(self.teach_dataset_path, 'frame_%06d.png'%(self.current_matched_teach_frame_id)))

                # Determine approximate patch location due to different scales
                scaled_patch_location = self.patch_center_location * (teach_img.shape[1] / self.IMAGE_COMPARISON_SIZE[0])

                # Draw comparison patch on current image and location on teach image
                DrawCropPatchOnImage(self.current_image, self.PATCH_PORTION)
                DrawCropPatchOnImage(teach_img, self.PATCH_PORTION, scaled_patch_location)

                cv.imshow('Repeat Image', self.current_image)
                cv.imshow('Matched Teach Image', teach_img)
                cv.waitKey(1)
                self.update_visualisation = False

    # ODOM CALLBACK
    def Odom_Callback(self, data):
        self.odom_topic_recieved = True
        self.current_odom = data.pose.pose

    # IMAGE CALLBACK
    def Image_Callback(self, data):
        # Only start process images once odom callback has run once
        if not self.odom_topic_recieved:
            rospy.logwarn('Waiting until odometry data is received. Make sure topic is published and topic name is correct.')
            return

        # Only process every nth frame
        self.frame_counter = (self.frame_counter + 1) % self.PROCESS_EVERY_NTH_FRAME
        if self.frame_counter != 0:
            return

        # Set first frame and previous odom if frame_id is 0
        if self.frame_id == 0:
            self.previous_odom = self.current_odom
            self.first_frame_odom = self.current_odom

        # Calculate relative odom from previous frame, and 
        # Calculate pose of current frame within the first image coordinate frame 
        # return type is a transformation matrix (4x4 numpy array)
        relative_odom_trans = CalculateTransformBetweenPoseMessages(self.current_odom, self.previous_odom)
        relative_pose_trans = CalculateTransformBetweenPoseMessages(self.current_odom, self.first_frame_odom)
        if relative_odom_trans.size == 0:
            rospy.loginfo('Unable to get relative odom transform. Make sure topic is published and topic name is correct.')
            # can still perform matching, as currently don't use the relative odom information
            

        # Attempt to convert ROS image into CV data type (i.e. numpy array)
        try:
            img_bgr = self.CV_BRIDGE.imgmsg_to_cv2(data, "bgr8")
            self.current_image = img_bgr.copy() # used for visualisation
        except Exception as e:
            rospy.logerr("Unable to convert ROS image into CV data. Error: " + str(e))
            return

        # Save repeat dataset if required
        if self.SAVE_REPEAT_DATA:
            retval = WriteDataToDatasetFile(img_bgr, self.frame_id, self.save_path, relative_odom_trans, relative_pose_trans, self.dataset_file, {'SAVE_IMAGE_RESIZE': self.SAVE_IMAGE_RESIZE})
            if retval == -1:
                rospy.logwarn("Was unable to save repeat image (ID = %d)"%(self.frame_id) + ". Error: " + str(e))

        # Image Matching
        match_teach_id, lateral_offset = self.ImageMatching(img_bgr, relative_odom_trans)
        # rospy.loginfo('Matched To Teach Frame ID: %d'%(match_teach_id))

        # Controller
        self.Controller(match_teach_id, lateral_offset)

        # Update frame ID and previous odom
        self.frame_id += 1
        self.previous_odom = self.current_odom


    # IMAGE MATCHING - THIS IS THE FUNCTION YOU WILL CREATE
    def ImageMatching(self, img_bgr, relative_odom_trans):
        # Setup comparison temp variables
        best_score = None
        best_max_location = None

        # STEP 1 - PREPROCESS IMAGE IF REQUIRED
        # Preprocess repeat image
        img_proc = cv.cvtColor(img_bgr, cv.COLOR_BGR2GRAY)
        img_proc = cv.resize(img_proc, self.IMAGE_COMPARISON_SIZE)

        # Take center patch of repeat image
        img_proc_patch = ImageCropCenter(img_proc, self.PATCH_PORTION)

        # STEP 2 - FIND BEST MATCH IN TEACH SET (here only searching in a local window, not a global search)
        # Loop through teach dataset within given search radius
        start_idx = int(max(self.current_matched_teach_frame_id-self.FRAME_SEARCH_WINDOW, 0))
        end_idx = int(min(self.current_matched_teach_frame_id+self.FRAME_SEARCH_WINDOW+1, self.teach_dataset.shape[0]))
        # rospy.loginfo('Start: %d, End: %d'%(start_idx, end_idx))
        for teach_frame_id in self.teach_dataset[start_idx:end_idx, 0]:
            # Read in teach processed img
            teach_img = cv.imread(os.path.join(self.teach_dataset_processed_path, 'frame_%06d.png'%(teach_frame_id)), cv.IMREAD_GRAYSCALE)

            # Compare using normalised cross correlation (OpenCV Template Matching Function)
            result = cv.matchTemplate(teach_img, img_proc_patch, cv.TM_CCOEFF_NORMED)

            # Get maximum value and its location
            min_val, max_val, min_location, max_location = cv.minMaxLoc(result)

            if best_score == None or best_score < max_val:
                best_score = max_val
                best_max_location = max_location
                self.current_matched_teach_frame_id = int(teach_frame_id)

        # STEP 3 - FIND THE POSITION OF THE REPEAT FRAME RELATIVE TO THE BEST MATCHED TEACH FRAME
        # offsets should be measured relative to the best matched frame coordinate frame, as in
        # x-offset should be positive if the car is in front of the best matched teach frame
        # y-offset should be positive if the car is to the left of the best matched teach frame
        # yaw-offset will be positive if car has rotated to the left

        # Max location is top_left want center (max location is x,y not y,x like image coordinates)
        self.patch_center_location = np.array([max_location[0], max_location[1]]) + np.array([img_proc_patch.shape[1]/2.0, img_proc_patch.shape[0]/2.0])
        
        # y-offset will be positive if car is to the left of the teach frame (i.e. the repeat patch was found on the right hand side of the teach image)
        y_offset = img_proc.shape[1]//2 - self.patch_center_location[0]
        
        # create offsets array [x-offset, y-offset, yaw-offset] - assume x-offset and yaw-offset are zero
        offsets = np.array([self.X_OFFSET_SCALE_FACTOR*0, self.Y_OFFSET_SCALE_FACTOR*y_offset, self.YAW_OFFSET_SCALE_FACTOR*0])

        # Update visualiation
        self.update_visualisation = True

        # return
        return self.current_matched_teach_frame_id, offsets

    # CONTROLLER
    def Controller(self, match_teach_id, offsets):
        # Adapted From Peter Corke's Textbook - Driving a Car-Like Robot to a Pose (pg. 106)

        # Get transform to go from current matched frame to target frame position
        goal_pos_relative_trans = self.RelativeTFBetweenFrames(match_teach_id, match_teach_id+self.TARGET_FRAME_LOOKAHEAD)
        if goal_pos_relative_trans.size == 0:
            # probably at last frame, publish zero speed command
            self.ackermann_cmd.drive.speed = 0
            self.ackermann_cmd_publisher.publish(self.ackermann_cmd)
            rospy.loginfo('Believe we are at the end of the teach path.')
            return
            # goal_pos_relative_trans = np.identity(4)

        # Add in transform due to offset from current matched frame
        quaternion = quaternion_from_euler(0, 0, offsets[2])
        
        lateral_pose = Pose()
        lateral_pose.position.x = offsets[0]
        lateral_pose.position.y = offsets[1]
        lateral_pose.orientation.x = quaternion[0]
        lateral_pose.orientation.y = quaternion[1]
        lateral_pose.orientation.z = quaternion[2]
        lateral_pose.orientation.w = quaternion[3]

        lateral_pose_trans = transform_tools.pose_msg_to_trans(lateral_pose)
        goal_pos_relative_trans = transform_tools.diff_trans(lateral_pose_trans, goal_pos_relative_trans)

        # Get distance (rho) and angle (alpha) to target frame position relative to current position, and
        # Get desired orientation (beta) wish to have at the target frame
        rho = transform_tools.distance_of_trans(goal_pos_relative_trans)
        alpha = np.arctan2(goal_pos_relative_trans[1,-1], goal_pos_relative_trans[0,-1])
        beta = transform_tools.yaw_from_trans(goal_pos_relative_trans)

        rospy.loginfo('Matched Frame ID: %d'%(match_teach_id))
        rospy.loginfo('Rho: %0.4f, alpha: %0.4f, Beta: %0.4f'%(rho, math.degrees(alpha), math.degrees(beta)))

        lin_vel = min(max(self.RHO_GAIN * rho, 0), self.MAX_FORWARD_VELOCITY)
        ang_vel = self.ALPHA_GAIN * alpha + self.BETA_GAIN * beta
        
        if lin_vel != 0:
            steering_angle = np.arctan(ang_vel * self.WHEEL_BASE / lin_vel)
        else:
            steering_angle = 0
        steering_angle = min(max(steering_angle, self.MIN_STEERING_ANGLE), self.MAX_STEERING_ANGLE)

        # Set values and publish message
        self.ackermann_cmd.drive.speed = lin_vel
        self.ackermann_cmd.drive.steering_angle = steering_angle
        self.ackermann_cmd_publisher.publish(self.ackermann_cmd)

    
    def RelativeTFBetweenFrames(self, current_frame_id, goal_frame_id):
        if current_frame_id == goal_frame_id:
            pose = Pose()
            pose.orientation.w = 1
            return transform_tools.pose_msg_to_trans(pose)

        relative_frame_tf = np.array([])
        # In the teach dataset want the relative pose from current frame to the goal frame. 
        # This data is stored in the current_frame+1 to goal_frame+1
        for row in self.teach_dataset[current_frame_id+1:goal_frame_id+1, 1:4]:

            # Get quaternion from yaw
            quaternion = quaternion_from_euler(0, 0, row[2])

            # Setup ROS Pose - will contain relative odom from previous teach frame
            frame_odom = Pose()
            frame_odom.position.x = row[0]
            frame_odom.position.y = row[1]
            frame_odom.orientation.x = quaternion[0]
            frame_odom.orientation.y = quaternion[1]
            frame_odom.orientation.z = quaternion[2]
            frame_odom.orientation.w = quaternion[3]

            # Get transform
            frame_tf = transform_tools.pose_msg_to_trans(frame_odom)

            # Build up relative tf
            if relative_frame_tf.size == 0:
                relative_frame_tf = frame_tf
            else:
                relative_frame_tf = transform_tools.append_trans(relative_frame_tf, frame_tf)

        return relative_frame_tf


### MAIN ####
if __name__ == "__main__":
    try:
        repeat = RepeatNode()
    except rospy.ROSInterruptException:
        if repeat.SAVE_REPEAT_DATA:
            repeat.dataset_file.close()
    
    if repeat.SAVE_REPEAT_DATA:
        repeat.dataset_file.close()
