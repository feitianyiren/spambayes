from __future__ import generators

import cPickle
import os
import sys
import errno
import shutil
import win32api, win32con, win32ui

import win32com.client
import win32com.client.gencache
import pythoncom

import config
import msgstore

try:
    True, False
except NameError:
    # Maintain compatibility with Python 2.2
    True, False = 1, 0

# Work out our "application directory", which is
# the directory of our main .py/.dll/.exe file we
# are running from.
try:
    if hasattr(sys, "frozen"):
        if sys.frozen == "dll":
            this_filename = win32api.GetModuleFileName(sys.frozendllhandle)
        else:
            # Don't think we will ever run as a .EXE, but...
            this_filename = os.path.abspath(sys.argv[0])
    else:
        this_filename = os.path.abspath(__file__)
except NameError: # no __file__
    this_filename = os.path.abspath(sys.argv[0])

# See if we can use the new bsddb module. (The old one is unreliable
# on Windows, so we don't use that)
try:
    import bsddb3 as bsddb
    # bsddb3 is definitely not broken
    use_db = True
except ImportError:
    # Not using the 3rd party bsddb3, so try the one in the std library
    try:
        import bsddb
        use_db = hasattr(bsddb, "db") # This name is not in the old one.
    except ImportError:
        # No DB library at all!
        use_db = False

# This is a little bit of a hack <wink>.  We are generally in a child
# directory of the bayes code.  To help installation, we handle the
# fact that this may not be on sys.path.  Note that doing these
# imports is delayed, so that we can set the BAYESCUSTOMIZE envar
# first (if we import anything from the core spambayes code before
# setting that envar, our .ini file may have no effect).
def import_core_spambayes_stuff(ini_filename):
    global bayes_classifier, bayes_tokenize, bayes_storage

    os.environ["BAYESCUSTOMIZE"] = ini_filename
    try:
        from spambayes import classifier
    except ImportError:
        parent = os.path.abspath(os.path.join(os.path.dirname(this_filename),
                                              ".."))
        sys.path.insert(0, parent)

    from spambayes import classifier
    from spambayes.tokenizer import tokenize
    from spambayes import storage
    bayes_classifier = classifier
    bayes_tokenize = tokenize
    bayes_storage = storage

class ManagerError(Exception):
    pass

class Stats:
    def __init__(self):
        self.num_seen = self.num_spam = self.num_unsure = 0

# Report a message to the user - should only be used for pretty serious errors
# hence we also print a traceback.
# Module level function so we can report errors creating the manager
def ReportError(message, title = None):
    import traceback
    print "ERROR:", message
    traceback.print_exc()
    if title is None:
        title = "SpamBayes Anti-Spam plugin"
    win32ui.MessageBox(message, title)

# Function to "safely" save a pickle, only overwriting
# the existing file after a successful write.
def SavePickle(what, filename):
    temp_filename = filename + ".tmp"
    file = open(temp_filename,"wb")
    try:
        cPickle.dump(what, file, 1)
    finally:
        file.close()
    # now rename to the correct file.
    try:
        os.unlink(filename)
    except os.error:
        pass
    os.rename(temp_filename, filename)

# Base class for our "storage manager" - we choose between the pickle
# and DB versions at runtime.  As our bayes uses spambayes.storage,
# our base class can share common bayes loading code.
class BasicStorageManager:
    db_extension = None # for pychecker - overwritten by subclass
    def __init__(self, bayes_base_name, mdb_base_name):
        self.bayes_filename = bayes_base_name + self.db_extension
        self.mdb_filename = mdb_base_name + self.db_extension
    def new_bayes(self):
        # Just delete the file and do an "open"
        try:
            os.unlink(self.bayes_filename)
        except EnvironmentError, e:
            if e.errno != errno.ENOENT: raise
        return self.open_bayes()
    def store_bayes(self, bayes):
        bayes.store()
    def open_bayes(self):
        raise NotImplementedError

class PickleStorageManager(BasicStorageManager):
    db_extension = ".pck"
    def open_bayes(self):
        return bayes_storage.PickledClassifier(self.bayes_filename)
    def close_bayes(self, bayes):
        pass
    def open_mdb(self):
        return cPickle.load(open(self.mdb_filename, 'rb'))
    def new_mdb(self):
        return {}
    def store_mdb(self, mdb):
        SavePickle(mdb, self.mdb_filename)
    def close_mdb(self, mdb):
        pass
    def is_incremental(self):
        return False # False means we always save the entire DB

class DBStorageManager(BasicStorageManager):
    db_extension = ".db"
    def open_bayes(self):
        return bayes_storage.DBDictClassifier(self.bayes_filename)
    def close_bayes(self, bayes):
        bayes.db.close()
        bayes.dbm.close()
    def open_mdb(self):
        return bsddb.hashopen(self.mdb_filename)
    def new_mdb(self):
        try:
            os.unlink(self.mdb_filename)
        except EnvironmentError, e:
            if e.errno != errno.ENOENT: raise
        return self.open_mdb()
    def store_mdb(self, mdb):
        mdb.sync()
    def close_mdb(self, mdb):
        mdb.close()
    def is_incremental(self):
        return True # True means only changed records get actually written

# Our main "bayes manager"
class BayesManager:
    def __init__(self, config_base="default", outlook=None, verbose=1):
        self.reported_startup_error = False
        self.config = None
        self.addin = None
        self.verbose = verbose
        self.stats = Stats()
        self.application_directory = os.path.dirname(this_filename)
        self.data_directory = self.LocateDataDirectory()
        self.MigrateDataDirectory()
        if not os.path.isabs(config_base):
            config_base = os.path.join(self.data_directory,
                                       config_base)
        config_base = os.path.abspath(config_base)

        self.ini_filename = config_base + "_bayes_customize.ini"
        self.config_filename = config_base + "_configuration.pck"

        # Read the configuration file.
        self.config = self.LoadConfig()

        self.outlook = outlook

        import_core_spambayes_stuff(self.ini_filename)

        bayes_base = config_base + "_bayes_database"
        mdb_base = config_base + "_message_database"
        # determine which db manager to use, and create it.
        ManagerClass = [PickleStorageManager, DBStorageManager][use_db]
        self.db_manager = ManagerClass(bayes_base, mdb_base)

        self.bayes = self.message_db = None
        self.LoadBayes()
        self.message_store = msgstore.MAPIMsgStore(outlook)

    def ReportError(self, message, title = None):
        ReportError(message, title)

    # Report a super-serious startup error to the user.
    # This should only be used when SpamBayes was previously working, but a
    # critical error means we are probably not working now.
    # We just report the first such error - subsequent ones are likely a result of
    # the first - hence, this must only be used for startup errors.
    def ReportFatalStartupError(self, message):
        if not self.reported_startup_error:
            self.reported_startup_error = True
            full_message = \
                "There was an error initializing the Spam plugin.\r\n\r\n" \
                "Spam filtering has been disabled.  Please re-configure\r\n" \
                "and re-enable this plugin\r\n\r\n" \
                "Error details:\r\n" + message
            # Disable the plugin
            if self.config is not None:
                self.config.filter.enabled = False
            self.ReportError(full_message)
        else:
            # We have reported the error, but for the sake of the log, we
            # still want it logged there.
            import traceback
            print "ERROR:", message
            traceback.print_exc()

    # Outlook used to give us thread grief - now we avoid Outlook
    # from threads, but this remains a worthwhile abstraction.
    def WorkerThreadStarting(self):
        pythoncom.CoInitialize()

    def WorkerThreadEnding(self):
        pythoncom.CoUninitialize()

    def LocateDataDirectory(self):
        # Locate the best directory for our data files.
        from win32com.shell import shell, shellcon
        try:
            appdata = shell.SHGetFolderPath(0,shellcon.CSIDL_APPDATA,0,0)
            path = os.path.join(appdata, "SpamBayes")
            if not os.path.isdir(path):
                os.makedirs(path)
            return path
        except pythoncom.com_error:
            # Function doesn't exist on early win95,
            # and it may just fail anyway!
            return self.application_directory
        except EnvironmentError:
            # Can't make the directory.
            return self.application_directory
            
    def MigrateDataDirectory(self):
        # A bit of a nod to save people doing a full retrain.
        # Try and locate our files in the old location, and move
        # then to the new one.
        # Also used first time SpamBayes is run - this will cause
        # the ini file to be *copied* to the correct directory
        self._MigrateFile("default_bayes_customize.ini", False)
        self._MigrateFile("default_bayes_database.pck")
        self._MigrateFile("default_bayes_database.db")
        self._MigrateFile("default_message_database.pck")
        self._MigrateFile("default_message_database.db")
        self._MigrateFile("default_configuration.pck")

    # Copy a file from the application_directory to the data_directory.
    # By default (do_move not specified), the source file is deleted.
    # Pass do_move=False to leave the original file.
    def _MigrateFile(self, filename, do_move = True):
        src = os.path.join(self.application_directory, filename)
        dest = os.path.join(self.data_directory, filename)
        if os.path.isfile(src) and not os.path.isfile(dest):
            # shutil in 2.2 and earlier don't contain 'move'.
            # Win95 and Win98 don't support MoveFileEx.
            shutil.copyfile(src, dest)
            if do_move:
                os.remove(src)

    def FormatFolderNames(self, folder_ids, include_sub):
        names = []
        for eid in folder_ids:
            folder = self.message_store.GetFolder(eid)
            if folder is None:
                name = "<unknown folder>"
            else:
                name = folder.name
            names.append(name)
        ret = '; '.join(names)
        if include_sub:
            ret += " (incl. Sub-folders)"
        return ret

    def EnsureOutlookFieldsForFolder(self, folder_id, include_sub=False):
        # Ensure that our fields exist on the Outlook *folder*
        # Setting properties via our msgstore (via Ext Mapi) gets the props
        # on the message OK, but Outlook doesn't see it as a "UserProperty".
        # Using MAPI to set them directly on the folder also has no effect.
        # So until we know better, use Outlook to hack this in.
        # Should be called once per folder you are watching/filtering etc
        #
        # Oh the tribulations of our property grail
        # We originally wanted to use the "Integer" Outlook field,
        # but it seems this property type alone is not expose via the Object
        # model.  So we resort to olPercent, and live with the % sign
        # (which really is OK!)
        assert self.outlook is not None, "I need outlook :("
        msgstore_folder = self.message_store.GetFolder(folder_id)
        if msgstore_folder is None:
            print "Checking a folder for our field failed - "\
                  "there is no such folder."
            return
            
        folder = msgstore_folder.GetOutlookItem()
        if self.verbose > 1:
            print "Checking folder '%s' for our field '%s'" \
                  % (self.config.field_score_name,folder.Name.encode("mbcs", "replace"))
        items = folder.Items
        item = items.GetFirst()
        while item is not None:
            if item.Class != win32com.client.constants.olMail:
                item = items.GetNext()
                continue
            break
        # OK - item is either a mail item, or None
        if item is not None:
            ups = item.UserProperties
            # *sigh* - need to search by int index
            for i in range(ups.Count):
                up = ups[i+1]
                if up.Name == self.config.field_score_name:
                    break
            else: # for not broken
                try:
                    # Display format is documented as being the 1-based index in
                    # the combo box in the outlook UI for the given data type.
                    # 1 is the first - "Rounded", which seems fine.
                    format = 1
                    ups.Add(self.config.field_score_name,
                           win32com.client.constants.olPercent,
                           True, # Add to folder
                           format)
                    item.Save()
                    if self.verbose > 1:
                        print "Created the UserProperty!"
                except pythoncom.com_error, details:
                    print "Warning: failed to create the Outlook " \
                          "user-property in folder '%s'" \
                          % (folder.Name.encode("mbcs", "replace"),)
                    print "", details
            if include_sub:
                # Recurse down the folder list.
                folders = item.Parent.Folders
                folder = folders.GetFirst()
                while folder is not None:
                    this_id = folder.StoreID, folder.EntryID
                    self.EnsureOutlookFieldsForFolder(this_id, True)
                    folder = folders.GetNext()
        # else no items in this folder - not much worth doing!

    def LoadBayes(self):
        import time
        start = time.clock()
        if not os.path.exists(self.ini_filename):
            raise ManagerError("The file '%s' must exist before the "
                               "database '%s' can be opened or created" % (
                               self.ini_filename, self.db_manager.bayes_filename))
        bayes = message_db = None
        try:
            # file-not-found handled gracefully by storage.
            bayes = self.db_manager.open_bayes()
            print "Loaded bayes database from '%s'" % (self.db_manager.bayes_filename,)
        except:
            self.ReportFatalStartupError("Failed to load bayes database")
        try:
            message_db = self.db_manager.open_mdb()
            print "Loaded message database from '%s'" % (self.db_manager.mdb_filename,)
        except IOError:
            pass
        except:
            self.ReportFatalStartupError("Failed to load bayes message database")
        if bayes is None or message_db is None:
            self.bayes = bayes
            self.message_db = message_db
            print "Either bayes database or message database is missing - creating new"
            self.InitNewBayes()
            bayes = self.bayes
            message_db = self.message_db
        if self.verbose:
            print ("Bayes database initialized with "
                   "%d spam and %d good messages" % (bayes.nspam, bayes.nham))
        if len(message_db) != bayes.nham + bayes.nspam:
            print "*** - message database has %d messages - bayes has %d - something is screwey" % \
                    (len(message_db), bayes.nham + bayes.nspam)
        self.bayes = bayes
        self.message_db = message_db
        self.bayes_dirty = False
        if self.verbose:
            print "Loaded databases in %gms" % ((time.clock()-start)*1000)

    def LoadConfig(self):
        # Our 'config' file always uses a pickle
        try:
            f = open(self.config_filename, 'rb')
        except IOError:
            if self.verbose:
                print ("Created new configuration file '%s'" %
                       self.config_filename)
            return config.ConfigurationRoot()

        try:
            ret = cPickle.load(f)
            f.close()
            if self.verbose > 1:
                print "Loaded configuration from '%s':" % self.config_filename
                ret._dump()
        except IOError, details:
            # File-not-found - less serious.
            ret = config.ConfigurationRoot()
            if self.verbose > 1:
                # filename included in exception!
                print "IOError loading configuration (%s) - using default:" % (details)
        except:
            # Any other error loading configuration is nasty, but should not
            # cause a fatal error.
            msg = "FAILED to load configuration from '%s'" % (self.config_filename,)
            self.ReportFatalStartupError(msg)
            ret = config.ConfigurationRoot()
        return ret

    def InitNewBayes(self):
        if self.bayes is not None:
            self.db_manager.close_bayes(self.bayes)
        if self.message_db is not None:
            self.db_manager.close_mdb(self.message_db)
        self.bayes = self.db_manager.new_bayes()
        self.message_db = self.db_manager.new_mdb()
        self.bayes_dirty = True

    def SaveBayesPostIncrementalTrain(self):
        # Save the database after a training operation - only actually
        # saves if we aren't using pickles.
        if self.db_manager.is_incremental():
            if self.bayes_dirty:
                self.SaveBayes()
            else:
                print "Bayes database is not dirty - not writing"
        else:
            print "Using a slow database - not saving after incremental train"


    def SaveBayes(self):
        import time
        start = time.clock()
        bayes = self.bayes
        # Try and work out where this count sometimes goes wrong.
        if bayes.nspam + bayes.nham != len(self.message_db):
            print "WARNING: Bayes database has %d messages, " \
                  "but training database has %d" % \
                  (bayes.nspam + bayes.nham, len(self.message_db))

        if self.verbose:
            print "Saving bayes database with %d spam and %d good messages" %\
                   (bayes.nspam, bayes.nham)
            print " ->", self.db_manager.bayes_filename
        self.db_manager.store_bayes(self.bayes)
        if self.verbose:
            print " ->", self.db_manager.mdb_filename
        self.db_manager.store_mdb(self.message_db)
        self.bayes_dirty = False
        if self.verbose:
            print "Saved databases in %gms" % ((time.clock()-start)*1000)

    def GetClassifier(self):
        """Return the classifier we're using."""
        return self.bayes

    def SaveConfig(self):
        if self.verbose > 1:
            print "Saving configuration:"
            self.config._dump()
            print " ->", self.config_filename
        assert self.config, "Have no config to save!"
        SavePickle(self.config, self.config_filename)

    def Save(self):
        # No longer save the config here - do it explicitly when changing it
        # (prevents lots of extra pickle writes, for no good reason.  Other
        # alternative is a dirty flag for config - this is simpler)
        if self.bayes_dirty:
            self.SaveBayes()
        else:
            print "Bayes database is not dirty - not writing"

    def Close(self):
        if self.bayes_dirty and self.bayes:
            print "Warning: BayesManager closed while Bayes database dirty"
        self.bayes = None
        self.config = None
        if self.message_store is not None:
            self.message_store.Close()
            self.message_store = None
        self.outlook = None

    def score(self, msg, evidence=False):
        """Score a msg.

        If optional arg evidence is specified and true, the result is a
        two-tuple

            score, clues

        where clues is a list of the (word, spamprob(word)) pairs that
        went into determining the score.  Else just the score is returned.
        """
        email = msg.GetEmailPackageObject()
        return self.bayes.spamprob(bayes_tokenize(email), evidence)

    def ShowManager(self):
        def do_train(dlg):
            import train
            import dialogs.TrainingDialog
            d = dialogs.TrainingDialog.TrainingDialog(dlg.mgr, train.trainer)
            d.DoModal()

        def do_filter(dlg):
            import filter
            import dialogs.FilterDialog
            d = dialogs.FilterDialog.FilterNowDialog(dlg.mgr, filter.filterer)
            d.DoModal()

        def define_filter(dlg):
            import filter
            import dialogs.FilterDialog
            d = dialogs.FilterDialog.FilterArrivalsDialog(dlg.mgr, filter.filterer)
            d.DoModal()
            if dlg.mgr.addin is not None:
                dlg.mgr.addin.FiltersChanged()


        import dialogs.ManagerDialog
        d = dialogs.ManagerDialog.ManagerDialog(self, do_train, do_filter, define_filter)
        d.DoModal()
        # And re-save now, just incase Outlook dies on the way down.
        self.SaveConfig()

_mgr = None

def GetManager(outlook = None, verbose=1):
    global _mgr
    if _mgr is None:
        if outlook is None:
            outlook = win32com.client.Dispatch("Outlook.Application")
        _mgr = BayesManager(outlook=outlook, verbose=verbose)
    # If requesting greater verbosity, honour it
    if verbose > _mgr.verbose:
        _mgr.verbose = verbose
    return _mgr

def ShowManager(mgr):
    mgr.ShowManager()

def main(verbose_level = 1):
    try:
        mgr = GetManager(verbose=verbose_level)
    except ManagerError, d:
        print "Error initializing Bayes manager"
        print d
        return 1
    ShowManager(mgr)
    mgr.Save()
    mgr.Close()
    return 0

def usage():
    print "Usage: manager [-v ...]"
    sys.exit(1)

if __name__=='__main__':
    verbose = 1
    import getopt
    opts, args = getopt.getopt(sys.argv[1:], "v")
    if args:
        usage()
    for opt, val in opts:
        if opt=="-v":
            verbose += 1
        else:
            usage()
    sys.exit(main(verbose))
