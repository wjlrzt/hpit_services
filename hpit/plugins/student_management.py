from hpitclient import Plugin

from pymongo import MongoClient
from bson.objectid import ObjectId

from threading import Timer

import time

from hpit.management.settings_manager import SettingsManager
settings = SettingsManager.get_plugin_settings()

from couchbase import Couchbase
import couchbase

import requests

class StudentManagementPlugin(Plugin):

    def __init__(self, entity_id, api_key, logger, args = None):
        super().__init__(entity_id, api_key) 
        self.logger = logger
        self.mongo = MongoClient(settings.MONGODB_URI)
        self.db = self.mongo[settings.MONGO_DBNAME].hpit_students
        
        self.TIMEOUT = 30
        self.student_model_fragment_names = ["knowledge_tracing","problem_management","hint_factory"]
        self.student_models = {}
        self.timeout_threads = {}
        
        
        try:
            self.cache = Couchbase.connect(bucket = "student_model_cache", host = settings.COUCHBASE_HOSTNAME)
        except couchbase.exceptions.BucketNotFoundError:
            options = {
                "authType":"sasl",
                "saslPassword":"",
                "bucketType":"memcached",
                "flushEnabled":1,
                "name":"student_model_cache",
                "ramQuotaMB":100,
            }
            req = requests.post(settings.COUCHBASE_BUCKET_URI,auth=settings.COUCHBASE_AUTH, data = options)
            
            self.cache = Couchbase.connect(bucket = "student_model_cache", host = settings.COUCHBASE_HOSTNAME)

    def post_connect(self):
        super().post_connect()
        
        self.subscribe({
            "tutorgen.add_student":self.add_student_callback,
            "tutorgen.get_student":self.get_student_callback,
            "tutorgen.set_attribute":self.set_attribute_callback,
            "tutorgen.get_attribute":self.get_attribute_callback,
            "tutorgen.get_student_model":self.get_student_model_callback})

    #Student Management Plugin
    def add_student_callback(self, message):
        if self.logger:
            self.send_log_entry("ADD_STUDENT")
            self.send_log_entry(message)
        
        try:
            attributes = message["attributes"]
        except KeyError:
            attributes = {}
        
        response = self._post_data("new-resource",{"owner_id":message["sender_entity_id"]}).json()
        resource_id = response["resource_id"]
        
        student_id = self.db.insert({"attributes":attributes,"resource_id":str(resource_id),"owner_id":str(message["sender_entity_id"])})
        
        self.send_response(message["message_id"],{"student_id":str(student_id),"attributes":attributes,"resource_id":str(resource_id)})
        
    def get_student_callback(self, message):
        if self.logger:
            self.send_log_entry("GET_STUDENT")
            self.send_log_entry(message)
        
        try:
            student_id = message["student_id"]
        except:
            self.send_response(message["message_id"],{"error":"Must provide a 'student_id' to get a student"})
            return
        
        return_student = self.db.find_one({"_id":ObjectId(student_id),"owner_id":str(message["sender_entity_id"])})
        if not return_student:
            self.send_response(message["message_id"],{"error":"Student with id " + str(student_id) + " not found."})
        else:
            self.send_response(message["message_id"],{"student_id":str(return_student["_id"]),"resource_id":str(return_student["resource_id"]),"attributes":return_student["attributes"]})
            
    def set_attribute_callback(self, message):
        if self.logger:
            self.send_log_entry("SET_ATTRIBUTE")
            self.send_log_entry(message)
        
        try:
            student_id = message["student_id"]
            attribute_name = message["attribute_name"]
            attribute_value = message["attribute_value"]
        except KeyError:
            self.send_response(message["message_id"],{"error":"Must provide a 'student_id', 'attribute_name' and 'attribute_value'"})
            return
            
        update = self.db.update({'_id':ObjectId(str(student_id)),"owner_id":str(message["sender_entity_id"])},{'$set':{'attributes.'+str(attribute_name): str(attribute_value)}},upsert=False, multi=False)
        if not update["updatedExisting"]:
            self.send_response(message["message_id"],{"error":"Student with id " + str(student_id) + " not found."})
        else:
            record = self.db.find_one({"_id":ObjectId(str(student_id))})
            self.send_response(message["message_id"],{"student_id":str(record["_id"]),"attributes":record["attributes"]})
               
    def get_attribute_callback(self, message):
        if self.logger:
            self.send_log_entry("GET_ATTRIBUTE")
            self.send_log_entry(message)
        
        try:
            student_id = message["student_id"]
            attribute_name = message["attribute_name"]
        except KeyError:
            self.send_response(message["message_id"],{"error":"Must provide a 'student_id' and 'attribute_name'"})
            return 
            
        student = self.db.find_one({'_id':ObjectId(str(student_id)),"owner_id":str(message["sender_entity_id"])})
        if not student:
            self.send_response(message["message_id"],{"error":"Student with id " + str(student_id) + " not found."})
            return
        else:
            try:
                attr = student["attributes"][attribute_name]
            except KeyError:
                attr = ""
            self.send_response(message["message_id"],{"student_id":str(student["_id"]),attribute_name:attr,"resource_id":student["resource_id"]})
 
    def get_student_model_callback(self,message):
        if self.logger:
            self.send_log_entry("GET_STUDENT_MODEL")
            self.send_log_entry(message)        
           
        if 'student_id' not in message:
            self.send_response(message["message_id"],{
                "error":"get_student_model requires a 'student_id'",         
            })
            return
        student_id = message["student_id"]
          
        student = self.db.find_one({'_id':ObjectId(str(message["student_id"])),"owner_id":str(message["sender_entity_id"])})
        if not student:
            self.send_response(message["message_id"],{
                "error":"student with id " + str(student_id) + " does not exist.",
            })
            return
          
        update = False 
        if "update" in message:
            update = message["update"]
            
        if not update:
            try:
                cached_model = self.cache.get(str(student_id)).value
                self.send_response(message["message_id"],{
                        "student_id": str(student_id),
                        "student_model" : cached_model,
                        "cached": True,
                        "resource_id":student["resource_id"]
                    })
                return
            except couchbase.exceptions.NotFoundError:
                pass

        self.student_models[message["message_id"]] = {}
        self.timeout_threads[message["message_id"]] = Timer(self.TIMEOUT, self.kill_timeout, [message, student_id])
        self.timeout_threads[message["message_id"]].start()

        self.send("get_student_model_fragment",{
                "update": update,
                "student_id" : str(message["student_id"]),
        },self.get_populate_student_model_callback_function(student_id,message))

        
    
    def get_populate_student_model_callback_function(self, student_id, message):
        def populate_student_model(response):
            
            #check if values exist
            try:
                name = response["name"]
                fragment = response["fragment"]
            except KeyError:
                return
            
            #check if name is valid
            if response["name"] not in self.student_model_fragment_names:
                return

            #fill student model
            try:
                self.student_models[message["message_id"]][response["name"]] = response["fragment"]
                if self.logger:
                    self.send_log_entry("GOT FRAGMENT " + str(response["fragment"]) + str(message["message_id"]))
                    
            except KeyError:
                return
            
            #check to see if student model complete
            for name in self.student_model_fragment_names:
                try:
                    if self.student_models[message["message_id"]][name] == None:
                        break
                except KeyError:
                    break
            else:
                #student model complete, send response (unless timed out)
                student = self.db.find_one({'_id':ObjectId(str(message["student_id"])),"owner_id":str(message["sender_entity_id"])})
                if message["message_id"] in self.timeout_threads:
                    self.send_response(message["message_id"], {
                        "student_id": str(student_id),
                        "student_model" : self.student_models[message["message_id"]],       
                        "cached":False,
                        "resource_id":student["resource_id"],
                        "message_id":str(message["message_id"]),
                    })
                   
                    try: 
                        self.cache.set(str(message["student_id"]), self.student_models[message["message_id"]])
                        
                        self.timeout_threads[message["message_id"]].cancel()
                        del self.timeout_threads[message["message_id"]]
                        del self.student_models[message["message_id"]]
                    except KeyError:
                        pass

                    return

        return populate_student_model
        
    def kill_timeout(self, message, student_id):
        if self.logger:
            self.send_log_entry("TIMEOUT " + str(message))
        
        student = self.db.find_one({'_id':ObjectId(str(message["student_id"])),"owner_id":str(message["sender_entity_id"])})
        
        try:
            self.send_response(message["message_id"],{
                "error":"Get student model timed out. Here is a partial student model.",
                'student_id': student_id,
                "student_model":self.student_models[str(message["message_id"])],
                "resource_id":student["resource_id"],
                "message_id":str(message["message_id"])
            })
        except KeyError:
            self.send_response(message["message_id"],{
                "error":"Get student model timed out. Here is a partial student model.",
                'student_id': student_id,
                "student_model":{},
                "resource_id":student["resource_id"],
                "message_id":str(message["message_id"])
            })
        
        try:
            del self.timeout_threads[message["message_id"]]
            del self.student_models[message["message_id"]]
        except KeyError:
            pass
        