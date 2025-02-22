# Implement the cvmfsmon API
# A URL is of the form:
#  /cvmfsmon/api/v1.0/montests&param1=value1&param2=value2
# The URL is parsed by the calling function, and the params come in a 
#   dictionary of keys, each with a list of values
# Currently supported "montests" are
#  ok - always returns OK
#  all - runs all applicable tests but 'ok'
#  updated - verifies that updates are happening on a stratum 1
#  gc - verifies that repositories that have done garbage collection
#       have done it successfully recently
#  geo - verifies that the geo api on a stratum 1 successfully
#       responds with a server order for a test case on one repository.
#       Also, it monitors geodb age.
#  whitelist - verifies that whitelist file is not expired
#  check - verifies that cvmfs_server check does not have failures
# Currently supported parameters are
#  format - value one of the following (default: list)
#    status - reports only one line: OK, WARNING, or CRITICAL
#    list - reports a line for each available status, followed by colon, 
#      followed by a comma-separated list of repositories at that status.
#    details - detailed json output with all the tests and messages
#  server - value is either 'local' (the default, indicating localhost) or
#    an alias of a server as defined in /etc/cvmfsmon/api.conf

from __future__ import print_function

import os, sys, socket, json, pprint, string
import time, threading
import cvmfsmon_updated, cvmfsmon_gc, cvmfsmon_geo, cvmfsmon_whitelist, cvmfsmon_check

try:
    from urllib import request as urllib_request
    from io import StringIO
except ImportError:  # python < 3
    import urllib2 as urllib_request
    from StringIO import StringIO

negative_expire_secs = 60*2         # 2 minutes
positive_expire_secs = 60*2         # 2 minutes
timeout_secs = 5                    # tries twice for 5 seconds
request_max_secs = 30               # maximum cache seconds when reading
config_update_time = 60             # seconds between checking config file

conf_mod_time = 0
last_config_time = 0
aliases = {}
excludes = []
disables = []
updated_slowrepos = []
limits = {}
lock = threading.Lock()

def error_request(start_response, response_code, response_body):
    response_body = response_body + '\n'
    response_body = response_body.encode('utf-8')
    start_response(response_code,
                   [('Cache-control', 'max-age=' + str(negative_expire_secs)),
                    ('Content-Length', str(len(response_body)))])
    return [response_body]

def bad_request(start_response, reason):
    response_body = 'Bad Request: ' + reason
    return error_request(start_response, '400 Bad Request', response_body)

def good_request(start_response, response_body):
    response_code = '200 OK'
    response_body = response_body.encode('utf-8')
    start_response(response_code,
                  [('Content-Type', 'text/plain'),
                   ('Cache-Control', 'max-age=' + str(positive_expire_secs)),
                   ('Content-Length', str(len(response_body)))])
    return [response_body]

def parse_api_conf():
    global aliases, excludes, disables, limits, updated_slowrepos
    global conf_mod_time
    global subdirectories
    conffile = '/etc/cvmfsmon/api.conf'
    try:
        modtime = os.stat(conffile).st_mtime
        if modtime == conf_mod_time:
            # no change
            return
        conf_mod_time = modtime

        aliases = { 'local' : '127.0.0.1' }
        excludes = []
        disables = []
        updated_slowrepos = []
        limits = {
            'updated-multiplier': 1.1,
            'updated-warning': 8,
            'updated-critical': 24,
            'gc-warning': 10,
            'gc-critical': 20,
            'whitelist-warning': 48
        }
        subdirectories = {}

        for line in open(conffile, 'r').read().split('\n'):
            words = line.split()
            if words:
                if words[0] == 'serveralias' and len(words) > 1:
                    parts = words[1].split('=')
                    subparts = parts[1].split('/', 1)
                    server = subparts[0]
                    aliases[parts[0]] = server
                    if len(subparts) > 1:
                        subdirectories[server] = subparts[1]
                elif words[0] == 'excluderepo':
                    excludes.append(words[1])
                elif words[0] == 'disabletest':
                    disables.append(words[1])
                elif words[0] == 'updated-slowrepo':
                    updated_slowrepos.append(words[1])
                elif words[0] == 'limit' and len(words) > 1:
                    parts = words[1].split('=')
                    if parts[0] == 'updated-multiplier':
                        limits[parts[0]] = float(parts[1])
                    else:
                        limits[parts[0]] = int(parts[1])

        print('processed ' + conffile)
        print('aliases: ' + str(aliases))
        print('excludes: ' + str(excludes))
        print('limits: ' + str(limits))
        print('subdirectories: ' + str(subdirectories))
        print('updated-slowrepos: ' + str(updated_slowrepos))
    except Exception as e:
        print('error reading ' + conffile + ', continuing: ' + str(e))
        conf_mod_time = 0

def domontest(testname, montests):
    if testname == montests:
        return True
    if montests == "all" and testname not in disables:
        return True
    return False

# from https://stackoverflow.com/a/55619288
# simulate python2 pretty printer by not breaking up strings
class Python2PrettyPrinter(pprint.PrettyPrinter):
    class _fake_short_str(str):
        def __len__(self):
            return 1 if super().__len__() else 0

    def format(self, object, context, maxlevels, level):
        res = super().format(object, context, maxlevels, level)
        if isinstance(object, str):
            return (self._fake_short_str(res[0]), ) + res[1:]
        return res

def dispatch(version, montests, parameters, start_response, environ):
    global last_config_time
    now = time.time()
    lock.acquire()
    if now - config_update_time > last_config_time:
        last_config_time = now
        parse_api_conf()
    lock.release()

    if 'server' in parameters:
        serveralias = parameters['server'][0]
    else:
        serveralias = 'local'
    if serveralias in aliases:
        server = aliases[serveralias]
    else:
        return bad_request(start_response, 'unrecognized server alias ' + serveralias)

    socket.setdefaulttimeout(timeout_secs)

    url = 'http://' + server + '/cvmfs/info/v1/repositories.json'
    replicas = []
    repos = []
    headers={"Cache-control" : "max-age=" + str(request_max_secs)}
    try:
        request = urllib_request.Request(url, headers=headers)
        json_data = urllib_request.urlopen(request).read().decode('utf-8')
        repos_info = json.loads(json_data)
        if 'replicas' in repos_info:
            for repo_info in repos_info['replicas']:
                if 'pass-through' not in repo_info or not repo_info['pass-through']:
                    # monitor it if it is not a pass-through mode replica
                    # the url always has the visible name
                    # use "str" to convert from unicode to string
                    replicas.append(str(repo_info['url'].replace('/cvmfs/','')))
        if 'repositories' in repos_info:
            for repo_info in repos_info['repositories']:
                repos.append(str(repo_info['url'].replace('/cvmfs/','')))
    except:
        return error_request(start_response, '502 Bad Gateway', url + ' error: ' + str(sys.exc_info()[1]))

    allresults = []
    if replicas and domontest('geo', montests):
        allresults.append(cvmfsmon_geo.runtest(replicas[0], server, headers, repos_info.get('last_geodb_update', '')))

    replicas_and_repos = []
    if montests != 'geo':
        replicas_and_repos = replicas + repos

    for repo in replicas_and_repos:
        if repo in excludes:
            continue
        if montests == 'ok':
            allresults.append([ 'ok', repo, 'OK', '' ])
            continue
        errormsg = ""
        doupdated = False
        if (repo in replicas) and domontest('updated', montests):
            doupdated = True
        repo_status = {}
        repourl = 'http://' + server + '/cvmfs/' + repo
        if server in subdirectories:
            repourl = repourl + '/' + subdirectories[server]
        url = repourl + '/.cvmfs_status.json'
        status_json = ""
        try:
            request = urllib_request.Request(url, headers=headers)
            status_json = urllib_request.urlopen(request).read().decode('utf-8')
            repo_status = json.loads(status_json)
        except urllib_request.HTTPError as e:
            if e.code == 404:
                if doupdated:
                    # for backward compatibility, look for .cvmfs_last_snapshot
                    #   if .cvmfs_status.json was not found
                    try:
                        url2 = repourl + '/.cvmfs_last_snapshot'
                        request = urllib_request.Request(url2, headers=headers)
                        snapshot_string = urllib_request.urlopen(request).read().decode('utf-8')
                        repo_status['last_snapshot'] = snapshot_string
                    except urllib_request.HTTPError as e:
                        if e.code == 404:
                            errormsg = url + ' and .cvmfs_last_snapshot Not found'
                        else:
                            errormsg =  str(sys.exc_info()[1])
                    except:
                        errormsg =  str(sys.exc_info()[1])
                else:
                    errormsg = url + ' Not found'
            else:
                errormsg =  str(sys.exc_info()[1])
        except:
            errormsg =  str(sys.exc_info()[1])

        results = []
        if domontest('check', montests):
            results.append(cvmfsmon_check.runtest(repo, repo_status, errormsg))

        if doupdated:
            if 'last_snapshot' not in repo_status:
                # no complete snapshot, look up snapshotting status
                try:
                    url2 = repourl + '/.cvmfs_is_snapshotting'
                    request = urllib_request.Request(url2, headers=headers)
                    snapshotting_string = urllib_request.urlopen(request).read().decode('utf-8')
                    repo_status['snapshotting_since'] = snapshotting_string
                except:
                    pass
            results.append(cvmfsmon_updated.runtest(repo, limits, repo_status, updated_slowrepos, errormsg))
        if domontest('gc', montests):
            results.append(cvmfsmon_gc.runtest(repo, limits, repo_status, errormsg))

        # clear any error message from above since it's no longer relevant
        errormsg = ""

        url_whitelist = repourl + '/.cvmfswhitelist'
        whitelist = ""
        try:
            request = urllib_request.Request(url_whitelist, headers=headers)
            whitelistdata = urllib_request.urlopen(request).read()
            # whitelistdata needs to be decoded on python3, but the binary
            # signature data can cause a decode error if the whole thing is
            # done at once, so decode line by line.
            for linedata in whitelistdata.splitlines():
                line = linedata.decode('utf-8')
                if line == '--':
                    break
                whitelist += line + '\n'
        except:
            errormsg =  str(sys.exc_info()[1])

        if domontest('whitelist', montests):
            results.append(cvmfsmon_whitelist.runtest(repo, limits, whitelist, errormsg))
        if results == []:
            return bad_request(start_response, 'unrecognized montests ' + montests)
        allresults.extend(results)

    format = 'list'
    if 'format' in parameters:
        formats = parameters['format']
        l = len(formats)
        if l > 0:
            format = formats[l - 1]

    body = ""
    if format == 'status':
        worststatus = 'OK'
        for result in allresults:
            if len(result) == 0:
                continue
            status = result[2]
            if status == 'CRITICAL':
                worststatus = 'CRITICAL'
            elif (status == 'WARNING') and (worststatus != 'CRITICAL'):
                worststatus = 'WARNING'
        body = worststatus + '\n'
    elif format == 'details':
        details = {}
        for result in allresults:
            if len(result) == 0:
                continue
            test = result[0]
            status = result[2]
            repomsg = {'repo' : result[1], 'msg': result[3]}
            if status in details:
                if test in details[status]:
                    details[status][test].append(repomsg)
                else:
                    details[status][test] = [repomsg]
            else:
                details[status] = {}
                details[status][test] = [repomsg]

        output = StringIO()
        Python2PrettyPrinter(stream=output).pprint(details)
        body = output.getvalue()
        output.close()
        body = body.replace("'", '"')
    else:  # list format
        repostatuses = {}
        for result in allresults:
            if len(result) == 0:
                continue
            repo = result[1]
            status = result[2]
            worststatus = 'OK'
            if repo in repostatuses:
                worststatus = repostatuses[repo]
            if status == 'CRITICAL':
                worststatus = status
            elif (status == 'WARNING') and (worststatus != 'CRITICAL'):
                worststatus = status
            repostatuses[repo] = worststatus

        statusrepos = {}
        for repo in repostatuses:
            status = repostatuses[repo]
            if not status in statusrepos:
                statusrepos[status] = []
            statusrepos[status].append(repo)
        for status in ['CRITICAL', 'WARNING', 'OK']:
            if status in statusrepos:
                statusrepos[status].sort()
                body += status + ':' + ",".join(statusrepos[status]) + '\n'

    return good_request(start_response, body)

