#!/apps/anaconda/bin/python3
###########################################################################################
# Job name: Restart Index API
# Description: This job will perform a rolling restart of the selected nodes.  
# Output:      Output goes to std out
# Date:        June 2023
# Email:       nicholas.j.giles@census.gov
###########################################################################################


import json
import os
import subprocess
import datetime
import time
import requests
requests.packages.urllib3.disable_warnings()
from collections import namedtuple
import re
from colorama import Fore


# Function to loop through curls to hosts/ports. This will print the results to console, and return the failed_curls dictionary of results. Dictionary format: {"host": ["port1", "port2", "port3"]}.
def check_service_port(env, server_function, service_type, expected_response = 200, uri = ""):

  # Define a dictionary to hold results
  failed_curls = {}
  
  # Loop through hosts for the given env, if it matches the applicable function. see inventories.json
  for host in inventory["environments"][env]["hosts"]:
    if host in inventory["functions"][server_function]["hosts"]:

      down_ports = []
      
      # Loop through ports for the applicable service(s).
      for service in inventory["environments"][env]["services"][service_type]:
        port = inventory["environments"][env]["services"][service_type][service]
        
        # Big nasty if condition to accomidate Elastic single instance hosts.
        if host in inventory["server_type"]["children"]["vm"]["hosts"] and server_function == "elastic_index_layer" and port != 9200:
          continue
        elif host in inventory["server_type"]["children"]["bm"]["hosts"] and server_function == "elastic_index_layer" and service == "elasticsearch.service":
          continue

        # Define the URL.
        url = "https://" + host + ":" + str(port)
        
        if uri:
          url += uri

        # Perform the curl cmd.
        curl = curl_get(url)

        # If the response is bad, print/store the results.
        if curl.status_code != expected_response:

          # Put the bad port into a list.
          down_ports.append(str(port))

          # Add the list to the host in the failed_curls dictionary.
          if host in failed_curls:
            failed_curls[host] = down_ports
          else:
            failed_curls.update({host: down_ports})
          
          # To console.
          print(Fore.RED + host + ":" + str(port) + " " + service + " FAILED the curl check in " + env + Fore.BLACK)
        else:
          # To console.
          print(host + ":" + str(port) + " " + service + " passed the curl check in " + env)

  return failed_curls


# Function for sending curl's as an HTTP get. The URL input is required. Headers and JSON data are optional. This returns name.status_code, name.body, and name.elapsed (response time).
def curl_get(url, headers={}, json = {}):
    
    # Open the curl command for the url.
    try:
        response = requests.get(url, headers=headers, json = json, timeout=60, verify=False)
    except requests.exceptions.ConnectionError:
        status_code = 0
        body = ""
        elapsed = ""
        result = namedtuple("result", ["status_code", "body"])
        return result(status_code, body)

    # Determine if the response body is JSON or text.
    if "application/json" in response.headers.get("content-type"):
        body = response.json()
    else:
        body = response.text

    status_code = response.status_code
    elapsed = response.elapsed

    # Make the response objects callable.
    result = namedtuple("result", ["status_code", "body", "elapsed"])

    return result(status_code, body, elapsed)


# Function to add a node into the F5 load balancing pool. Run the "server_profile" function prior to this, to get the applicable input variable in the proper format. This will return true or false based on the ssh command exit code.
def f5_node_insert(host, node, function, monitor_file):
  
  # Shorten the service file name to insert it into the monitor file path.
    path_node = re.search("(?<=@)(.*)\.", node)
    

    # Build the path to the monitor file, based on if the host is serving the index or data api.
    if "data_access_layer" in function:
        mon_file_path = "/data/tomcat/base.d/" + path_node.group(1) + "/webapps/ROOT/" + monitor_file
    elif "middle_tier_and_ui" in function:
        mon_file_path = "/data/tomcat/base/" + path_node.group(1) + "/webapps/ROOT/" + monitor_file


    # Run the ssh command to switch the monitor.json file from false to true.
    node_insert_cmd = subprocess.run("ssh -q -t " + host + " sed -i '0,/false/s//true/'" + " " + mon_file_path,
            shell=True, capture_output=True, text=True)
    
    return node_insert_cmd.returncode


# Function to check the status of a nodes monitor file. Run the "server_profile" function prior to this, to get the applicable input variable in the proper format. This will return true or false based on the ssh command exit code.
def f5_node_status(host, node, function, monitor_file):
   
   # Shorten the service file name to insert it into the monitor file path.
    path_node = re.search("(?<=@)(.*)\.", node)
    

    # Build the path to the monitor file, based on if the host is serving the index or data api.
    if "data_access_layer" in function:
        mon_file_path = "/data/tomcat/base.d/" + path_node.group(1) + "/webapps/ROOT/" + monitor_file
    elif "middle_tier_and_ui" in function:
        mon_file_path = "/data/tomcat/base/" + path_node.group(1) + "/webapps/ROOT/" + monitor_file


    # Run the ssh command to check if the monitor file shows true.
    f5_status_cmd = subprocess.run("ssh -q -t " + host + " grep -q 'true' " + mon_file_path, shell=True, capture_output=True, text=True)
    
    return f5_status_cmd.returncode


# Function to start, stop, restart, or retrieve the status of a node on a remote host. This returns the ssh command exit code, any possible eror messages, whether or not the node was removed from the F5 pool, or a service file status. Additionally, this will print a message if the node is being removed from the F5 pool in order to perform the action. Run the "server_profile" function prior to this, to get the applicable input variable in the proper format.
def node_action_command(host, node, action, split_env, monitor_file, function):    
     
    # Define the functions minimal output.
    action_cmd_result = bool
    error = ""
    f5_removal = False
    status = ""
    shortened_node = re.search(".+(?=\.service)", node)
    
    # If the host is a web server in a split env and not just checking status.
    if ((function == "data_access_layer") or (function == "middle_tier_and_ui")) and (split_env == "True") and (action != "status"):
        
        # Reformat the node name for the monitor file path and the action command
        path_node = re.search("(?<=@)(.*)\.", node)
       
        # If index or data api hosts, build the path to the monitor file.
        if "data_access_layer" in function:
            mon_file_path = "/data/tomcat/base.d/" + path_node.group(1) + "/webapps/ROOT/" + monitor_file
        elif "middle_tier_and_ui" in function:
            mon_file_path = "/data/tomcat/base/" + path_node.group(1) + "/webapps/ROOT/" + monitor_file
           
        # Check if the node is currently live.
        f5_node_check = subprocess.run("ssh -q -t " + host + " grep 'true' " + mon_file_path, shell=True, capture_output=True, text=True)
        
        # If the host is currently live > remove the host from the load balancing pool.
        if '"active": true' in f5_node_check.stdout:
           
            # Update the f5 removal output.
            f5_removal = True
            
            # Console update.
            print(shortened_node + " is being removed from the F5 pool to perform the action.")
            
            # Run the ssh command to remove the host from the F5 pool.
            node_removal_cmd = subprocess.run("ssh -q -t " + host + " sed -i '0,/true/s//false/' " + mon_file_path,
                    shell=True, capture_output=True, text=True)
            
        # If the command to check if the node is live fails.
        elif f5_node_check.returncode != 0:
           
            # If the F5 node check fails
            error = "The command to check if the node is live " + Fore.RED + "failed." + Fore.BLACK + " The " + action + " command will not be performed."
            # Console update.
            print(error)

        # If the node was successfully removed from the F5 pool or was never live to begin with.
        if (node_removal_cmd.returncode == 0) or ('"active": false' in f5_node_check.stdout):
            
            # Run the ssh action command.
            action_cmd = subprocess.run("ssh -q -t " + host + " sudo systemctl " + action + " " + node + " || sudo systemctl " + action + " " + str(shortened_node), shell=True, capture_output=True, text=True)

            # If the action command failed
            if action_cmd == False:
               
               action_cmd_result = False
               error = "The " + action + " command " + Fore.RED + "FAILED" + Fore.BLACK + ". check the node."
            
            else:
               
               # Success.
               action_cmd_result = True
            
        else:
           
           # The F5 removal command failed.
           action_cmd_result = False
           error = "The command to remove " + Fore.RED + host + ": " + node + Fore.BLACK + " from the F5 pool " + Fore.RED + "FAILED." + Fore.BLACK     

    # If the action command is not status.     
    elif action != "status":
        
        # Run the action command. 
        # format: ssh -q -t <fqdn> "sudo systemctl restart tomcat@node1.service || sudo systemctl restart tomcat@node1"
        action_cmd = subprocess.run("ssh -q -t " + host + ' "sudo systemctl ' + action + " " + node + " || sudo systemctl " + action + " " + str(shortened_node.group()) + '"', shell=True, capture_output=True, text=True)
        
        # If the action command was successful.
        if action_cmd.returncode == 0:
           # Success
           action_cmd_result = True
        else:
           # Fail
           action_cmd_result = False
           error = "The " + action + " command " + Fore.RED + "failed" + Fore.BLACK + ". Check the node."
           
    # If the function is checking status.
    elif action == "status":

       # If the service file is active.
        action_cmd = subprocess.run("ssh -q -t " + host + " systemctl is-active " + node + " || systemctl is-active " + str(shortened_node), shell=True, capture_output=True, text=True)

        if action_cmd.returncode:
           status = "Active"
        else:
           status = "Inactive"


    # Set the action command results as callable variables.   
    output = namedtuple("output", ["action_cmd_result", "error", "f5_removal", "status"])

    return output(action_cmd_result, error, f5_removal, status)


# Function to determine a hosts environment, monitor file, and whether the hosts environment is split or not. The Input is a hostname. The output is a tuple of ["env", "split_env", "monitor_file", function]
def server_profile(hostname):
   
   # Define some info about the host.
    env = ""
    split_env = ""
    monitor_file = ""
    function = []

    # Define the applicable environments.
    prodops_envs = ["prod_a", "prod_b", "embargo", "at", "preprod", "fr_a", "fr_b", "er_a", "er_b"]

    # For each environemnt in the inventory file, if the hostname exists under the hosts. 
    for environment in prodops_envs:
       if hostname in inventory["environments"][environment]["hosts"]:
          
          # Extract variables from the matches.
          env = environment
          # Helps determine monitor.json, and whether or not a node needs to be removed from the F5 pool.
          split_env = inventory["environments"][environment]["split"]
          monitor_file = inventory["environments"][environment]["monitor_file"]
       else:
          error = "The host was not found in any environment."


    # Determine the hosts function. 
    for serv_type in inventory["functions"]:
       if hostname in inventory["functions"][serv_type]["hosts"]:
          function.append(serv_type)
       else:
          error = "The hosts function could not be found."


    # If all variables were assigned.
    if env and split_env and monitor_file and function:

        # Make the variables callable.
        result = namedtuple("result", ["env", "split_env", "monitor_file", "function"])
        return result(env, split_env, monitor_file, function)
    else:
       return error


# Function to loop through curls to a single host, port, or service. The default time between curls is 10 seconds. duration determines the number of curls to be sent. 35 curls x 10 seconds = 5.8 minutes of attempts. expected_response format [http response code, response time, expected body].
def curl_loop(host, port, uri, expected_response = [200, 60, True], duration = 35):

    # Set variables for the loop and results
    curl_count = 0
    success = False
    reason = ""

    # While the loop is true
    while curl_count < duration:

        # Assemble the URL.
        url = "https://" + host + ":" + str(port) + uri

        # If this is the first curl check > wait 3 minutes after the check.
        if curl_count == 0:

            # Sleep for 15 seconds for the service to come up.
            time.sleep(15)

            # Run a curl to the host
            curl_response = curl_get(url, headers={}, json = {})

            # Wait 3 minutes for the NER index to generate.
#            time.sleep(180)
        
        # Run a curl to the host
        curl_response = curl_get(url, headers={}, json = {})

        # If the curl responds as expected. 
        if curl_response.status_code == expected_response[0] and curl_response.body == expected_response[2] and curl_response.elapsed < expected_response[1]:  
           # Return success
           success = True
           break

        else:
            # Ensure there are atleast 10 seconds between curls, and increment the curl count.
            time.sleep(10)
            curl_count += 1
            reason += "Status Code: " + str(curl_response.status_code) + "\nResponse Time: " + curl_response.elapsed + "\nResponse Body: " + curl_response.body
           
    result = namedtuple("result", ["success", "reason"])

    return result(success, reason)


# Function to collect Jenkins parameter selections (lists) into a dictionary. This helps parse the restart job node selections into a dctionary that can be looped through. format: {"host": ["node1", "node2"]}
def selected_node_modifier(param_lists):
    
    # Define a dictionary to hold the selected parameters.
    selected_restarts = {}
    current_node = 0 

    # Loop through each node in the node_list.
    for parameter in param_lists:
    
        # If any hosts were selected for the given node parameter.
        if parameter:

            # Re-format the node parameter from one long string into a split list of hosts.
            node_parameter = parameter.split(",")
        
            # Loop through the node parameter for each host. 
            for host in node_parameter:
                
                # Set node to the string name of the current "node" parameter. ex. "node1" or "node2"
                node_name = "tomcat@" + switch_statement(current_node) + ".service"
                
                # If the host is not in the dictionary yet.
                if host not in selected_restarts:
                    # Insert the host and the node.
                    selected_restarts.update({host: [node_name]})
                else:
                    # append the node only.
                    selected_restarts[host].append(node_name)

        current_node += 1

    return selected_restarts


# Functions serves as a generic switch/case statement. Match/case is an upgraded functions which will be evailable if we ever get python 3.10.
def switch_statement(item):

    if item == 0:
       return "node1"
    if item == 1:
       return "node2"
    if item == 2:
       return "node3"
    if item == 3:
       return "node4"
    if item == 4:
       return "node7"
    if item == 5:
       return "node10"
       

#####################################
#           MAIN
#####################################


# Load inventory file; callable as "inventory".
with open("Scripts/inventory/inventories.json", "r") as inventory_file:
    inventory = json.load(inventory_file)


# Declare variables to pull in Jenkins parameters.
env = os.getenv("env")
action = os.getenv("action")
node1 = os.getenv("node1")
node2 = os.getenv("node2")
node3 = os.getenv("node3")
node4 = os.getenv("node4")
node7 = os.getenv("node7")
node10 = os.getenv("node10")
check_nodes = os.getenv("check_nodes")
node_list = [node1, node2, node3, node4, node7, node10]


# Convert the selected hosts/nodes into an iterable dictionary.
selected_restarts = selected_node_modifier(node_list)


# Create a dictionary to hold results.
action_results = {}


# Print a header to the Jenkins console.
print("*************************************************** \n   Performing the " + Fore.BLUE + action + Fore.BLACK + " command on Selected Nodes \n***************************************************")


# loop through the hosts for the given environment.
for host in selected_restarts:
    
    # Call "server_profile" to determine info about the server.
    host_profile = server_profile(host)
    
    # Print a host header to the Jenkins console.
    print(Fore.BLUE + "\n*********** " + host + " ***********" + Fore.BLACK)


    # Loop through the selected nodes for the current host. 
    for node in selected_restarts[host]:
        
        # Console update.
        print("\nPerforming the " + action + " command on " + node)

        # Call the action command function. This will remove the node from the F5 pool if applicable.
        action_result = node_action_command(host, node, action, split_env = host_profile.split_env, monitor_file = host_profile.monitor_file, function = host_profile.function)


        # If the action command was successful and the node does not need to be checked.
        if (action_result.action_cmd_result == True) and (check_nodes == "No") and (action != "status"):
           
            # Print success to the Jenkins console.
            print("The " + action + " command was successful.") # \nA single query will be sent to the node to initialize the NER index.")

            # Determine the port for the current node.
            port = inventory["environments"][host_profile.env]["services"]["index"][node]
            uri = "/api/search/webpages?keyWords=population"

            # Loop through curls to check the service functionality.
#            curl_check = curl_loop(host, port, uri, expected_response = [200, 60, True], duration = 1)


        # If the action command was "status".
        elif (action == "status"):
           
            # If active.
            if action_result.status == "Active":
           
                # Print success to the Jenkins console and continue to the next node.
                print("The " + action + " command shows Active.")
                continue
        
            # If not active.
            elif action_result.status != "Active":
            
                # Update the console.
                print("The status command shows " + node + " is " + Fore.RED + "NOT ACTIVE" + Fore.BLACK + ". Check the node")


        # The action command failed.
        elif action_result.action_cmd_result == False:
           
           # To console.
           print(action_result.error)
               
            
        # If the hostname is already in the removed_nodes dictionary.
        if host in action_results:
            
            # If the node is already listed for the host.
            if node in action_results[host]:
                continue

            else:
                removed_nodes[host].append(node)

        # The host is not in the dictionary yet.
        else:

            # Add the host and node to the dictionary.
            removed_nodes.update({host: {node: [action_result.action_cmd_result]}})            


# If the check_nodes UI selection is "Yes".
if (action_result.action_cmd_result == True) and (check_nodes == "Yes") and (action != "status"):
   
    # Print the restart result to Jenkins console.
    print("The " + action + " command was successful.\n The node will now go through checks for the proper response via curl.")

    # Determine the port for the current node.
    port = inventory["environments"][host_profile.env]["services"]["index"][node]
    uri = "/api/search/webpages?keyWords=population"

    # Loop through curls to check the service functionality.
    curl_check = curl_loop(host, port, uri, expected_response = [200, 60, True], duration = 35)

    # If the node responds as expected,
    if curl_check.success:
        
        # Print success to the Jenkins console.
        print("The service is responding as intended.")

    else:
        print(Fore.RED + "The curl checks FAILED after the " + action + "\n" + curl_check.reason + Fore.BLACK)


# If the node was removed from the F5 pool.
if (action_result.f5_removal == True) and (action_result.action_cmd_result == True):

    # If the node successfully passed curl checks.
    if ((check_nodes == "Yes") and (curl_check.success == True)) or (check_nodes == "No"):
        
        # To console.
        print("The node is being placed back into the F5 load balancing pool.")

        # Put the node back into the F5 pool.
        f5_insert_cmd = f5_node_insert(host, node, host_profile.function, host_profile.monitor_file)

        # If the command to add the node back into the F5 pool fails.
        if f5_insert_cmd != 0:
            print(Fore.RED + "The command to add the node back into the F5 pool FAILED. Check the monitor file status." + Fore.BLACK)

        else:
            print("Adding the node back into the F5 pool was a success. Verify the node via Heartbeat.")


# To do
# Move the check from each node restart to outside the whole big loop.
# Integrate the muting job. Should it be something "we" do or should it be placed in the jenkinsfile? Or this script could run a curl to trigger the alert muting job.
