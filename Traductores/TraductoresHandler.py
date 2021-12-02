# coding=utf-8

import json
import Configberry
import importlib
import socket
import threading
import tornado.ioloop
import os
from multiprocessing import Process, Queue, Pool

import sys
if sys.platform == 'win32':
    import multiprocessing.reduction    # make sockets pickable/inheritable


INTERVALO_IMPRESORA_WARNING = 30.0
import logging

logger = logging.getLogger(__name__)


def set_interval(func, sec):
    def func_wrapper():
        set_interval(func, sec)
        func()

    t = threading.Timer(sec, func_wrapper)
    t.start()
    return t


# es un diccionario como clave va el nombre de la impresora que funciona como cola
# cada KEY es una printerName y contiene un a instancia de TraductorReceipt o TraductorFiscal dependiendo
# si la impresora es fiscal o receipt

class TraductorException(Exception):
    pass



def init_printer_traductor(printerName):
    config = Configberry.Configberry()

    try:
        dictSectionConf = config.get_config_for_printer(printerName)
    except KeyError as e:
        raise TraductorException("En el archivo de configuracion no existe la impresora: '%s'" % printerName)

    marca = dictSectionConf.get("marca")
    del dictSectionConf['marca']
    # instanciar los comandos dinamicamente
    libraryName = "Comandos." + marca + "Comandos"
    comandoModule = importlib.import_module(libraryName)
    comandoClass = getattr(comandoModule, marca + "Comandos")
    
    comando = comandoClass(**dictSectionConf)
    return comando.traductor

def runTraductor(jsonTicket, queue):
    logging.info("mandando comando de impresora")
    print(jsonTicket)
    printerName = jsonTicket['printerName']
    traductor = init_printer_traductor(printerName)

    if traductor:
        if traductor.comando.conector is not None:
            queue.put(traductor.run(jsonTicket))
        else:
            strError = "el Driver no esta inicializado para la impresora %s" % printerName
            queue.put(strError)
            logging.error(strError)




class TraductoresHandler:
    """Convierte un JSON a Comando Fiscal Para Cualquier tipo de Impresora fiscal"""

    traductores = {}
    fbApp = None

    config = Configberry.Configberry()
    webSocket = None

    def __init__(self, webSocket = None, fbApp = None):
        self.webSocket = webSocket
        self.fbApp = fbApp




    def json_to_comando(self, jsonTicket):
        import time        
        traductor = None
        
        try:
            """ leer y procesar una factura en formato JSON
            """
            logging.info("Iniciando procesamiento de json:::: "+json.dumps(jsonTicket))

            rta = {"rta": ""}
            # seleccionar impresora
            # esto se debe ejecutar antes que cualquier otro comando
            if 'printerName' in jsonTicket:
                # run multiprocessing
                q = Queue()
                p = Process(target=runTraductor, args=(jsonTicket,q))
                p.daemon = True
                #p = MultiprocesingTraductor(traductorhandler=self, jsonTicket=jsonTicket, q=q)
                p.start()
                p.join()
                if q.empty() == False:
                    rta["rta"] = q.get(timeout=1)
                q.close()

            # aciones de comando genericos de Ststus y control
            elif 'getStatus' in jsonTicket:
                rta["rta"] = self._getStatus()

            # reinicia
            elif 'reboot' in jsonTicket:
                rta["rta"] = self._reboot()

            elif 'restart' in jsonTicket:
                rta["rta"] = self._restartService()

            elif 'upgrade' in jsonTicket:
                rta["rta"] = self._upgrade()

            elif 'getPrinterInfo' in jsonTicket:
                rta["rta"] =  self._getPrinterInfo(jsonTicket["getPrinterInfo"])

            elif 'findAvaliablePrinters' in jsonTicket:
                self._findAvaliablePrinters()
                rta["rta"] = self._getAvaliablePrinters()

            elif 'getAvaliablePrinters' in jsonTicket:
                rta["rta"] = self._getAvaliablePrinters()

            elif 'getActualConfig' in jsonTicket:
                rta["rta"] = self._getActualConfig()

            elif 'configure' in jsonTicket:
                rta["rta"] = self._configure(**jsonTicket["configure"])

            elif 'removerImpresora' in jsonTicket:
                rta["rta"] =  self._removerImpresora(jsonTicket["removerImpresora"])

            else:

                logger.error("No se pasó un comando válido")
                raise TraductorException("No se pasó un comando válido")

            # cerrar el driver
            if traductor and traductor.comando:
                traductor.comando.close()

            return rta

        except Exception, e:
            # cerrar el driver
            if traductor and traductor.comando:
                traductor.comando.close()

            raise

    def getWarnings(self):
        """ Recolecta los warning que puedan ir arrojando las impresoraas
            devuelve un listado de warnings
        """
        collect_warnings = {}
        for trad in self.traductores:
            if self.traductores[trad]:
                warn = self.traductores[trad].comando.getWarnings()
                if warn:
                    collect_warnings[trad] = warn
        return collect_warnings

    def _upgrade(self):
        ret = self.fbApp.upgradeGitPull()
        print(ret)
        rta = {
            "rta": ret
        }
        self.fbApp.restart_service()
        return rta

    def _getPrinterInfo(self, printerName):
        rta = {
            "printerName": printerName,
            "action": "getPrinterInfo",
            "rta": self.config.get_config_for_printer(printerName)
        }
        print(rta)
        return rta

    def _restartService(self):
        """ Reinicializa el WS server tornado y levanta la configuracion nuevamente """
        self.fbApp.restart_service()
        resdict = {
            "action": "restartService",
            "rta": "servidor reiniciado"
        }

    def _rebootFiscalberry(self):
        "reinicia el servicio fiscalberry"
        from subprocess import call

        resdict = {
            "action": "rebootFiscalberry",
            "rta": call(["reboot"])
        }

        return resdict

    def _configure(self, **kwargs):
        "Configura generando o modificando el archivo configure.ini"
        printerName = kwargs["printerName"]
        propiedadesImpresora = kwargs
        if "nombre_anterior" in kwargs:
            self._removerImpresora(kwargs["nombre_anterior"])
            del propiedadesImpresora["nombre_anterior"]
        del propiedadesImpresora["printerName"]
        self.config.writeSectionWithKwargs(printerName, propiedadesImpresora)

        return {
            "action": "configure",
            "rta": "La seccion "+printerName+" ha sido guardada"
        }

    def _removerImpresora(self, printerName):
        "elimina la sección del config.ini"

        self.config.delete_printer_from_config(printerName)

        return {
            "action": "removerImpresora",
            "rta": "La impresora "+printerName+" fue removida con exito"
        }


    def _findAvaliablePrinters(self):
        # Esta función llama a otra que busca impresoras. Luego se encarga de escribir el config.ini con las impresoras encontradas.
        if os.geteuid() != 0:
            return {"action": "findAvaliablePrinters",
                    "rta": "Error, no es superusuario (%s)" % os.geteuid()
                    }

        self.__getPrintersAndWriteConfig()

    def _getAvaliablePrinters(self):

        # la primer seccion corresponde a SERVER, el resto son las impresoras
        rta = {
            "action": "getAvaliablePrinters",
            "rta": self.config.sections()[1:]
        }

        return rta

    def _getStatus(self, *args):

        resdict = {"action": "getStatus", "rta": {}}
        for tradu in self.traductores:
            if self.traductores[tradu]:
                resdict["rta"][tradu] = "ONLINE"
            else:
                resdict["rta"][tradu] = "OFFLINE"
        return resdict

    def __manejar_socket_error(self, err, jsonTicket, traductor):
        print(format(err))
        traductor.comando.conector.driver.reconnect()
        # volver a intententar el mismo comando
        try:
            rta["rta"] = traductor.run(jsonTicket)
            return rta
        except Exception:
            # ok, no quiere conectar, continuar sin hacer nada
            print("No hay caso, probe de reconectar pero no se pudo")

    def _getActualConfig(self):
        rta = {
            "action": "getActualConfig",
            "rta": self.config.get_actual_config()
        }

        return rta
