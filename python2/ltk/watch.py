import ctypes
from ltk.actions import Action
from ltk.logger import logger
from ltk.utils import map_locale, restart
import time
import requests
from requests.exceptions import ConnectionError
import os
import sys
from watchdog.observers import Observer
from watchdog.events import FileSystemEvent
from ltk.watchhandler import WatchHandler

# retry decorator to retry connections
def retry(logger, timeout=5, exec_type=None):
    if not exec_type:
        exec_type = [requests.exceptions.ConnectionError]

    def decorator(function):
        def wrapper(*args, **kwargs):
            while True:
                try:
                    return function(*args, **kwargs)
                except Exception as e:
                    if e.__class__ in exec_type:
                        logger.error("Connection has timed out. Retrying..")
                        time.sleep(timeout)  # sleep for some time then retry
                    else:
                        raise e
        return wrapper
    return decorator

def is_hidden_file(file_path):
    # todo more robust checking for OSX files that doesn't start with '.'
    name = os.path.basename(os.path.abspath(file_path))
    return name and (name.startswith('.') or has_hidden_attribute(file_path) or name == "4913")

def has_hidden_attribute(file_path):
    """ Detects if a file has hidden attributes """
    try:
        # Python 2
        attrs = ctypes.windll.kernel32.GetFileAttributesW(unicode(file_path))
        # Python 3
#         attrs = ctypes.windll.kernel32.GetFileAttributesW(str(file_path))
        assert attrs != -1
        result = bool(attrs & 2)
    except (AttributeError, AssertionError):
        result = False
    return result

class WatchAction(Action):
    # def __init__(self, path, remote=False):
    def __init__(self, path=None, timeout=60):
        Action.__init__(self, path, True, timeout)
        self.observer = Observer()  # watchdog observer that will watch the files
        self.handler = WatchHandler()
        self.handler.on_modified = self._on_modified
        self.handler.on_created = self._on_created
        self.handler.on_moved = self._on_moved
        self.watch_queue = []  # not much slower than deque unless expecting 100+ items
        self.locale_delimiter = None
        self.ignore_ext = []  # file types to ignore as specified by the user
        self.detected_locales = {}  # dict to keep track of detected locales
        self.watch_folder = True
        self.timeout = timeout
        self.updated = {}
        # if remote:  # poll lingotek cloud periodically if this option enabled
        # self.remote_thread = threading.Thread(target=self.poll_remote(), args=())
        # self.remote_thread.daemon = True
        # self.remote_thread.start()

    def check_remote_doc_exist(self, fn, document_id=None):
        """ check if a document exists remotely """
        if not document_id:
            entry = self.doc_manager.get_doc_by_prop('file_name', fn)
            document_id = entry['id']
        response = self.api.get_document(document_id)
        if response.status_code != 200:
            return False
        return True

    def _on_modified(self, event):
        """ Notify Lingotek cloud when a previously added file is modified """
        try:
            db_entries = self.doc_manager.get_all_entries()
            in_db = False
            fn = ''
            # print event.src_path
            for entry in db_entries:
                # print entry['file_name']
                if event.src_path.endswith(entry['file_name']):
                    fn = entry['file_name']
                    in_db = True
            if not event.is_directory and in_db:
                try:
                    # check that document is added in TMS before updating
                    if self.check_remote_doc_exist(fn) and self.doc_manager.is_doc_modified(fn, self.path):
                        # logger.info('Detected local content modified: {0}'.format(fn))
                        # self.update_document_action(os.path.join(self.path, fn))
                        # logger.info('Updating remote content: {0}'.format(fn))
                        self.update_content(fn)
                except KeyboardInterrupt:
                    self.observer.stop()
                except ConnectionError:
                    print("Could not connect to remote server.")
                    restart()
                except ValueError:
                    print(sys.exc_info()[1])
                    restart()
        except KeyboardInterrupt:
            self.observer.stop()
        except Exception as err:
            restart("Error on modified: "+str(err)+"\nRestarting watch.")

    def _on_created(self, event):
        # get path
        # add action
        try:
            file_path = event.src_path
            # if it's a hidden document, don't do anything 
            if not is_hidden_file(file_path):
                relative_path = file_path.replace(self.path, '')
                title = os.path.basename(os.path.normpath(file_path))
                curr_ext = os.path.splitext(file_path)[1]
                # return if the extension should be ignored or if the path is not a file
                if curr_ext in self.ignore_ext or not os.path.isfile(file_path):
                    # logger.info("Detected a file with an extension in the ignore list, ignoring..")
                    return
                # only add or update the document if it's not a hidden document and it's a new file
                try:
                    if self.doc_manager.is_doc_new(relative_path) and self.watch_folder:
                        self.add_document(file_path, title, locale=self.locale)
                    elif self.doc_manager.is_doc_modified(relative_path, self.path):
                        self.update_content(relative_path)
                    else:
                        return
                except KeyboardInterrupt:
                    self.observer.stop()
                except ConnectionError:
                    print("Could not connect to remote server.")
                    restart()
                except ValueError:
                    print(sys.exc_info()[1])
                    restart()
                doc = self.doc_manager.get_doc_by_prop('name', title)
                if doc:
                    document_id = doc['id']
                else:
                    return
                if self.locale_delimiter:
                    try:
                        # curr_locale = title.split(self.locale_delimiter)[1]
                        # todo locale detection needs to be more robust
                        curr_locale = title.split(self.locale_delimiter)[-2]
                        fixed_locale = map_locale(curr_locale)
                        if fixed_locale:
                            print ("fixed locale: ", fixed_locale)
                            # self.watch_locales.add(fixed_locale)
                            self.detected_locales[document_id] = fixed_locale
                        else:
                            logger.warning('This document\'s detected locale: {0} is not supported.'.format(curr_locale))
                    except IndexError:
                        logger.warning('Cannot detect locales from file: {0}, not adding any locales'.format(title))
                self.watch_add_target(title, document_id)
                # logger.info('Added new document {0}'.format(title
            # else:
            #     print("Skipping hidden file "+file_path)
        except KeyboardInterrupt:
            self.observer.stop()
        # except Exception as err:
        #     restart("Error on created: "+str(err)+"\nRestarting watch.")

    def _on_moved(self, event):
        """Used for programs, such as gedit, that modify documents by moving (overwriting)
        the previous document with the temporary file. Only the moved event contains the name of the
        destination file."""
        try:
            event = FileSystemEvent(event.dest_path)
            self._on_modified(event)
        except KeyboardInterrupt:
            self.observer.stop()
        except Exception as err:
            restart("Error on moved: "+str(err)+"\nRestarting watch.")

    def get_watch_locales(self, document_id):
        """ determine the locales that should be added for a watched doc """
        locales = []
        if self.detected_locales:
            try:
                locales = [self.detected_locales[document_id]]
            except KeyError:
                logger.error("Something went wrong. Could not detect a locale")
            return locales
        entry = self.doc_manager.get_doc_by_prop("id", document_id)
        try:
            locales = [locale for locale in self.watch_locales if locale not in entry['locales']]
        except KeyError:
            locales = self.watch_locales
        return locales

    def watch_add_target(self, file_name, document_id):
        # print "watching add target, watch queue:", self.watch_queue
        if not file_name:
            title=self.doc_manager.get_doc_by_prop("id", document_id)
        else:
            title = os.path.basename(file_name)
        if document_id not in self.watch_queue:
            self.watch_queue.append(document_id)
        # Only add target if doc exists on the cloud
        if self.check_remote_doc_exist(title, document_id):
            locales_to_add = self.get_watch_locales(document_id)
            # if len(locales_to_add) == 1:
            #     printStr = "Adding target "+locales_to_add[0]
            # else:
            #     printStr = "Adding targets "
            #     for target in locales_to_add:
            #         printStr += target+","
            # print(printStr)
            self.target_action(title, file_name, locales_to_add, None, None, None, document_id)
            if document_id in self.watch_queue:
                self.watch_queue.remove(document_id)

    def process_queue(self):
        """do stuff with documents in queue (currently just add targets)"""
        # todo may want to process more than 1 item every "poll"..
        # if self.watch_queue:
        #     self.watch_add_target(None, self.watch_queue.pop(0))
        for document_id in self.watch_queue:
            self.watch_add_target(None, document_id)

    def update_content(self, relative_path):
        if self.update_document_action(os.path.join(self.path, relative_path)):
            self.updated[relative_path] = 0
            logger.info('Updating remote content: {0}'.format(relative_path))

    def check_modified(self, doc): # Checks if the version of a document on Lingotek's system is more recent than the local version
        old_date = doc['last_mod']
        response = self.api.get_document(doc['id'])
        if response.status_code == 200:
            new_date = response.json()['properties']['modified_date']
            # orig_count = response.json()['entities'][1]['properties']['count']['character']
        else:
            print("Document not found on Lingotek Cloud: "+str(doc['name']))
            return False
        if int(old_date)<int(str(new_date)[0:10]):
            return True
        return False

    @retry(logger)
    def poll_remote(self):
        """ poll lingotek servers to check if translation is finished """
        documents = self.doc_manager.get_all_entries()  # todo this gets all documents, not necessarily only ones in watch folder
        for doc in documents:
            doc_id = doc['id']
            if doc_id in self.watch_queue:
                # if doc id in queue, not imported yet
                continue
            file_name = doc['file_name']
            # Wait for Lingotek's system to no longer show translationas as completed
            if file_name in self.updated:
                if self.updated[file_name] > 3:
                    self.updated.pop(file_name, None)
                else:
                    self.updated[file_name] += self.timeout
                    continue
            locale_progress = self.import_locale_info(doc_id, True)
            try:
                downloaded = doc['downloaded']
            except KeyError:
                downloaded = []
                self.doc_manager.update_document('downloaded', downloaded, doc_id)
            # Python 2
            for locale, progress in locale_progress.iteritems():
            # Python 3
#             for locale in locale_progress:
#                 progress = locale_progress[locale]
                if progress == 100 and locale not in downloaded:
                    
                    logger.info('Translation completed ({0} - {1})'.format(doc_id, locale))
                    if self.locale_delimiter:
                        self.download_action(doc_id, locale, False, False)
                    else:
                        self.download_action(doc_id, locale, False)
                elif progress != 100 and locale in downloaded:
                    # print("Locale "+str(locale)+" for document "+doc['name']+" is no longer completed.")
                    self.doc_manager.remove_element_in_prop(doc_id, 'downloaded', locale)


    def complete_path(self, file_location):
        # print("self.path: "+self.path)
        # print("file_location: "+file_location)
        # print("abs file_location: "+os.path.abspath(file_location))
        abspath=os.path.abspath(file_location)
        # norm_path = os.path.abspath(file_location).replace(self.path, '')
        return abspath

    def watch_action(self, watch_path, ignore, delimiter=None):
        # print self.path
        if watch_path:  # Use watch path specified as an option/parameter
            self.watch_folder = True
        elif self.watch_dir and not "--default" in self.watch_dir: # Use watch path from config
            self.watch_folder = True
            watch_path = self.watch_dir
        else:
            watch_path = self.path # Watch from the project root
            self.watch_folder = False # Only watch added files
        if self.watch_folder:
            watch_path = self.complete_path(watch_path)
            print ("Watching for updates in: {0}".format(watch_path))
        else:
            print ("Watching for updates to added documents")
        self.ignore_ext.extend(ignore)
        self.locale_delimiter = delimiter
        self.observer.schedule(self.handler, path=watch_path, recursive=True)
        self.observer.start()
        queue_timeout = 3
        # start_time = time.clock()
        try:
            while True:
                self.poll_remote()
                current_timeout = self.timeout
                while len(self.watch_queue) and current_timeout > 0:
                    self.process_queue()
                    time.sleep(queue_timeout)
                    current_timeout -= queue_timeout
                time.sleep(self.timeout)
        except KeyboardInterrupt:
            self.observer.stop()
        # except Exception as err:
        #     restart("Error: "+str(err)+"\nRestarting watch.")

        self.observer.join()
