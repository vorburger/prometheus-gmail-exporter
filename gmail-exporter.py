#!/usr/bin/python3
"""
Checks gmail labels for unread messages and exposes the counts via prometheus.
"""

import os
import sys
from time import sleep
import logging
from functools import lru_cache

import httplib2
import configargparse

from prometheus_client import make_wsgi_app, Gauge

from flask import Flask, Response

import waitress

from threading import Thread

from werkzeug.middleware.dispatcher import DispatcherMiddleware

from googleapiclient import discovery
from oauth2client import client
from oauth2client.file import Storage

GMAIL_CLIENT = None
READINESS = ""
THREAD_SENDER_CACHE = dict()

app = Flask("prometheus-gmail-exporter")

def get_homedir_filepath(filename):
    config_dir = os.path.join(os.path.expanduser("~"), ".prometheus-gmail-exporter")

    if not os.path.exists(config_dir):
        os.mkdir(config_dir)

    return os.path.join(config_dir, filename)

def get_credentials():
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.
    """

    while not os.path.exists(args.clientSecretFile):
        set_readiness("Waiting for client secret file")
        logging.fatal("Client secrets file does not exist: %s . You probably need to download this from the Google API console.", args.clientSecretFile)
        sleep(10)

    credentials_path = args.credentialsPath

    store = Storage(credentials_path)
    credentials = store.get()

    if not credentials or credentials.invalid:
        scopes = 'https://www.googleapis.com/auth/gmail.readonly '

        flow = client.flow_from_clientsecrets(args.clientSecretFile, scopes)
        flow.user_agent = 'prometheus-gmail-exporter'

        credentials = run_flow(flow, store)

        logging.info("Storing credentials to %s", credentials_path)

    return credentials

def run_flow(flow, store):
    flow.redirect_uri = 'http://'
    authorize_url = flow.step1_get_authorize_url()

    logging.info("Go and authorize at: %s", authorize_url)

    if sys.stdout.isatty():
        code = input('Enter code:').strip()
    else:
        logging.info("Waiting for code at %s", get_homedir_filepath('auth_code'))

        while True:
            try:
                if os.path.exists(get_homedir_filepath('auth_code')):
                    with open(get_homedir_filepath('auth_code'), 'r') as auth_code_file:
                        code = auth_code_file.read()
                        break

            except Exception as e:
                logging.critical(e)

            set_readiness("Waiting for auth code")
            sleep(10)

    try:
        credential = flow.step2_exchange(code)
    except client.FlowExchangeError as e:
        logging.fatal("Auth failure: %s", str(e))
        sys.exit(1)

    set_readiness("")

    store.put(credential)
    credential.set_store(store)

    return credential

@lru_cache(maxsize=1)
def get_labels():
    """
    Note that this func is cached (lru_cache) and will only run once.
    """

    logging.info("Getting metadata about labels")

    labels = []

    if len(args.labels) == 0:
        logging.warning("No labels specified, assuming all labels. If you have a lot of labels in your inbox you could hit API limits quickly.")
        results = GMAIL_CLIENT.users().labels().list(userId='me').execute()

        labels = results.get('labels', [])
    else:
        logging.info('Using labels: %s ', args.labels)

        for label in args.labels:
            labels.append({'id': label})

    if not labels:
        logging.info('No labels found.')
        sys.exit()

    return labels

gauge_collection = {}

def get_gauge_for_label(name, desc, labels = None):
    if labels is None:
        labels = []

    if name not in gauge_collection:
        gauge = Gauge('gmail_' + name, desc, labels)
        gauge_collection[name] = gauge

    return gauge_collection[name]

def update_gauages_from_gmail(*unused_arguments_needed_for_scheduler):
    logging.info("Updating gmail metrics - started")

    for label in get_labels():
        try:
            label_info = GMAIL_CLIENT.users().labels().get(id=label['id'], userId='me').execute()

            gauge = get_gauge_for_label(label_info['id'] + '_total', label_info['name']  + ' Total')
            gauge.set(label_info['threadsTotal'])

            gauge = get_gauge_for_label(label_info['id'] + '_unread', label_info['name'] + ' Unread')
            gauge.set(label_info['threadsUnread'])

            if label['id'] in args.labelsSenderCount:
                update_sender_gauges_for_label(label_info['id'])

        except Exception as e:
            # eg, if this script is started with a label that exists, that is then deleted
            # after startup, 404 exceptions are thrown.
            #
            # Occsionally, the gmail API will throw garbage, too. Hence the try/catch.
            logging.error("Error: %s", e)

    logging.info("Updating gmail metrics - complete")

def get_first_message_sender(thread):
    if thread is None or thread['messages'] is None:
        return "unknown-thread-no-messages"

    firstMessage = thread['messages'][0]

    for header in firstMessage['payload']['headers']:
        if header['name'] == 'From':
            return header['value']

    return "unknown-no-from"

def get_all_threads_for_label(labelId):
    logging.info("get_all_threads_for_label - this method can be expensive: %s", str(labelId))

    response = GMAIL_CLIENT.users().threads().list(userId = 'me', labelIds = [labelId], q = "is:unread").execute()

    threads = []

    logging.info("get_all_threads_for_label - result size estimate: %s", str(response['resultSizeEstimate']))

    if "threads" in response:
        threads.extend(response['threads'])

    while "nextPageToken" in response:
        page_token = response['nextPageToken']
        response = GMAIL_CLIENT.users().threads().list(userId = 'me', labelIds = [labelId], pageToken = page_token, q = "is:unread").execute()
        threads.extend(response['threads'])

        logging.info("Getting more threads for label %s: %s", labelId, str(len(threads)))

    return threads

def get_thread_messages(thread):
    logging.info("Fetching thread messages for %s", str(thread['id']))

    res = GMAIL_CLIENT.users().threads().get(userId = 'me', id = thread['id'], format = "metadata").execute()

    thread['messages'] = res['messages']

    return thread

def update_sender_gauges_for_label(label):
    global THREAD_SENDER_CACHE

    senderCounts = dict()

    for thread in get_all_threads_for_label(label):
        if thread['id'] not in THREAD_SENDER_CACHE:
            thread = get_thread_messages(thread)

            THREAD_SENDER_CACHE[thread['id']] = get_first_message_sender(thread)

        sender = THREAD_SENDER_CACHE[thread['id']]

        if sender not in senderCounts:
            senderCounts[sender] = 0

        senderCounts[sender] += 1

    for sender, messageCount in senderCounts.items():
        g = get_gauge_for_label(label + '_sender', 'Label sender info', ['sender'])
        g.labels(sender=sender).set(messageCount)

def get_gmail_client():
    credentials = get_credentials()
    http_client = credentials.authorize(httplib2.Http())
    return discovery.build('gmail', 'v1', http=http_client)

def infinate_update_loop():
    while True:
        update_gauages_from_gmail()
        sleep(args.updateDelaySeconds)


def start_waitress():
    waitress.serve(app, host = "0.0.0.0", port = args.promPort)

def set_readiness(v):
    global READINESS

    READINESS = v

@app.route("/readyz")
def readyz():
    global READINESS

    if READINESS == "":
        return "OK"
    else: 
        return Response(READINESS, status = 503)

@app.route("/")
def index():
    return "prometheus-gmail-exporter"

def main(): 
    logging.getLogger().setLevel(args.logLevel)

    logging.info("prometheus-gmail-exporter starting on port %d", args.promPort)

    # Register prometheus (cannot do this after start())
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
        '/metrics': make_wsgi_app()
    })

    # Get the /readyz endpoint up as quickly as possible
    t = Thread(target = start_waitress)
    t.start()

    global GMAIL_CLIENT
    GMAIL_CLIENT = get_gmail_client()
 
    if args.daemonize: 
        infinate_update_loop()
    else:
        update_gauages_from_gmail()

    t.join()

if __name__ == '__main__':
    global args
    parser = configargparse.ArgumentParser(default_config_files=[
        get_homedir_filepath('prometheus-gmail-exporter.cfg'),
        get_homedir_filepath('prometheus-gmail-exporter.yaml'),
        "/etc/prometheus-gmail-exporter.cfg",
        "/etc/prometheus-gmail-exporter.yaml",
    ], config_file_parser_class=configargparse.YAMLConfigFileParser)

    parser.add_argument('--labels', nargs='*', default=[])
    parser.add_argument("--labelsSenderCount", nargs='*', default=[])
    parser.add_argument('--clientSecretFile', default=get_homedir_filepath('client_secret.json'))
    parser.add_argument('--credentialsPath', default=get_homedir_filepath('login_cookie.dat'))
    parser.add_argument("--updateDelaySeconds", type=int, default=300)
    parser.add_argument("--promPort", type=int, default=8080)
    parser.add_argument("--daemonize", "-D", action='store_true')
    parser.add_argument("--logLevel", type=int, default = 20)
    args = parser.parse_args()

    try:
        main()
    except KeyboardInterrupt:
        print("\n") # Most terminals print a Ctrl+C message as well. Looks ugly with our log.
        logging.info("Ctrl+C, bye!")
        sys.exit(0)
