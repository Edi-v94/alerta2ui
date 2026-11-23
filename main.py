"""Alerta2UI - alerta quando um dispositivo cadastrado fica fora do ar."""
import base64
import ctypes
import hashlib
import hmac
import io
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import uuid
import winreg
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from tkinter import filedialog, messagebox

import comtypes
import comtypes.client
import customtkinter as ctk
import pystray
from comtypes import CLSCTX_ALL
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image
from ping3 import ping
from pycaw.pycaw import IAudioEndpointVolume, IMMDeviceEnumerator

# --- CAMINHOS ---
APPDATA_DIR = os.path.join(os.getenv("APPDATA"), "Alerta2UI")
os.makedirs(APPDATA_DIR, exist_ok=True)
ARQUIVO_CONFIG = os.path.join(APPDATA_DIR, "config.dat")  # criptografado com DPAPI
ARQUIVO_CONFIG_ANTIGO = os.path.join(APPDATA_DIR, "config.json")  # versões antigas, em texto puro

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(os.path.join(APPDATA_DIR, "alerta2ui.log"), encoding="utf-8")],
)

INTERVALO_VERIFICACAO = 10  # segundos entre cada rodada de pings
COR_ONLINE = "#43A047"
COR_OFFLINE = "#E53935"
COR_FORA_TOLERANCIA = "#FB8C00"
COR_AGUARDANDO = "#9E9E9E"
COR_CARTAO = "#2B2B2B"
COR_TEXTO_SECUNDARIO = "#A0A7AD"
FORMATOS_SOM = [("Áudio/Vídeo", "*.mp4 *.mp3 *.wav *.m4a *.wma"), ("Todos", "*.*")]
UNIDADES_TEMPO = {"segundos": 1, "minutos": 60, "horas": 3600}


def resource_path(relativo):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relativo)


try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Alerta2UI.Monitoramento.1")
except Exception:
    pass

# --- CONFIGURAÇÃO / PERSISTÊNCIA ---
config = {"som": "", "volume": 100, "iniciar_windows": False, "dispositivos": [],
          "senha_hash": "", "senha_sal": ""}


# Criptografia DPAPI do Windows: só o mesmo usuário do Windows, neste computador, consegue abrir.
# Não há chave no código-fonte; ela é gerenciada pelo próprio Windows.
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


_ENTROPIA_DPAPI = b"Alerta2UI-config-v1"


def _blob(dados):
    buf = ctypes.create_string_buffer(dados, len(dados))
    return _DATA_BLOB(len(dados), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(func, dados):
    entrada, _b1 = _blob(dados)
    entropia, _b2 = _blob(_ENTROPIA_DPAPI)
    saida = _DATA_BLOB()
    if not func(ctypes.byref(entrada), None, ctypes.byref(entropia), None, None, 0x1, ctypes.byref(saida)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(saida.pbData)


def criptografar(dados):
    return _dpapi(ctypes.windll.crypt32.CryptProtectData, dados)


def descriptografar(dados):
    return _dpapi(ctypes.windll.crypt32.CryptUnprotectData, dados)


def carregar_config():
    if os.path.exists(ARQUIVO_CONFIG):
        try:
            with open(ARQUIVO_CONFIG, "rb") as f:
                config.update(json.loads(descriptografar(f.read()).decode("utf-8")))
        except Exception as e:
            # Ex.: arquivo copiado de outro usuário/PC. Guarda o arquivo em vez de sobrescrevê-lo.
            reserva = f"{ARQUIVO_CONFIG}.invalido-{int(time.time())}"
            logging.error(f"Falha ao abrir configuração ({e}); arquivo movido para {reserva}")
            os.replace(ARQUIVO_CONFIG, reserva)
    elif os.path.exists(ARQUIVO_CONFIG_ANTIGO):
        # Migra a configuração em texto puro para o formato criptografado e apaga a versão aberta
        try:
            with open(ARQUIVO_CONFIG_ANTIGO, encoding="utf-8") as f:
                config.update(json.load(f))
            salvar_config()
            os.remove(ARQUIVO_CONFIG_ANTIGO)
            logging.info("Configuração migrada para o formato criptografado.")
        except Exception as e:
            logging.error(f"Falha ao migrar configuração antiga: {e}")


def salvar_config():
    dados = criptografar(json.dumps(config, ensure_ascii=False).encode("utf-8"))
    temporario = ARQUIVO_CONFIG + ".tmp"
    with open(temporario, "wb") as f:
        f.write(dados)
    os.replace(temporario, ARQUIVO_CONFIG)  # troca atômica: não corrompe se faltar energia


CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_windows_ativo():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN) as chave:
            winreg.QueryValueEx(chave, "Alerta2UI")
            return True
    except OSError:
        return False


def configurar_startup_windows(ativar):
    chave_run = CHAVE_RUN
    exe = sys.executable
    if exe.lower().endswith(("python.exe", "pythonw.exe")):
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        comando = f'"{pythonw if os.path.exists(pythonw) else exe}" "{os.path.abspath(__file__)}"'
    else:
        comando = f'"{exe}"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chave_run, 0, winreg.KEY_ALL_ACCESS) as chave:
            if ativar:
                winreg.SetValueEx(chave, "Alerta2UI", 0, winreg.REG_SZ, comando)
            else:
                try:
                    winreg.DeleteValue(chave, "Alerta2UI")
                except FileNotFoundError:
                    pass
    except Exception as e:
        logging.error(f"Falha ao configurar início com o Windows: {e}")


carregar_config()

# --- SENHA ---
def _hash_senha(senha, sal_hex):
    return hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal_hex), 200_000).hex()


def definir_senha(senha):
    sal = os.urandom(16).hex()
    config["senha_sal"] = sal
    config["senha_hash"] = _hash_senha(senha, sal)
    salvar_config()


def senha_correta(senha):
    if not config.get("senha_hash"):
        return False
    return hmac.compare_digest(_hash_senha(senha, config["senha_sal"]), config["senha_hash"])


# --- SOM (MCI do Windows: toca mp4, mp3, wav, m4a, wma) ---
_mci = ctypes.windll.winmm.mciSendStringW
_som_tocando = False


def _mci_cmd(comando):
    erro = _mci(comando, None, 0, None)
    if erro:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.winmm.mciGetErrorStringW(erro, buf, 256)
        raise RuntimeError(buf.value)


def tocar_som(arquivo=None):
    global _som_tocando
    if _som_tocando:
        return
    arquivo = arquivo or config.get("som")
    if not arquivo or not os.path.exists(arquivo):
        return
    try:
        _mci_cmd(f'open "{arquivo}" type mpegvideo alias alerta')
        _mci_cmd("setaudio alerta volume to 1000")
        _mci_cmd("play alerta repeat")
        _som_tocando = True
    except Exception as e:
        logging.error(f"Falha ao tocar som de alerta: {e}")
        _mci("close alerta", None, 0, None)


def parar_som():
    global _som_tocando
    _mci("stop alerta", None, 0, None)
    _mci("close alerta", None, 0, None)
    _som_tocando = False


# --- VOLUME DO WINDOWS: travado no valor configurado enquanto houver alerta ---
forcar_volume = False  # ligado/desligado pela thread principal


def _obter_controle_volume():
    enumerador = comtypes.client.CreateObject("{BCDE0395-E52F-467C-8E3D-C4579291692E}",
                                              interface=IMMDeviceEnumerator)
    dispositivo = enumerador.GetDefaultAudioEndpoint(0, 1)  # saída de áudio padrão
    interface = dispositivo.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return ctypes.cast(interface, ctypes.POINTER(IAudioEndpointVolume))


def vigia_de_volume():
    comtypes.CoInitialize()
    volume = None
    anterior = None  # (nível, mudo) antes do alerta, para restaurar depois
    while rodando:
        try:
            if forcar_volume:
                if volume is None:
                    volume = _obter_controle_volume()
                    anterior = (volume.GetMasterVolumeLevelScalar(), volume.GetMute())
                alvo = config.get("volume", 100) / 100.0
                if volume.GetMute():
                    volume.SetMute(0, None)
                if abs(volume.GetMasterVolumeLevelScalar() - alvo) > 0.005:
                    volume.SetMasterVolumeLevelScalar(alvo, None)
            elif volume is not None:
                if anterior:
                    volume.SetMasterVolumeLevelScalar(anterior[0], None)
                    volume.SetMute(anterior[1], None)
                volume, anterior = None, None
        except Exception as e:
            logging.warning(f"Erro ao controlar o volume do Windows: {e}")
            volume = None  # tenta obter o dispositivo de novo (ex.: fone conectado)
        time.sleep(0.2)
    if volume is not None and anterior:  # programa encerrado durante um alerta
        try:
            volume.SetMasterVolumeLevelScalar(anterior[0], None)
            volume.SetMute(anterior[1], None)
        except Exception:
            pass


# --- MONITORAMENTO ---
# estado[id] = {"online": bool|None, "primeira_falha": float|None, "texto": str}
estado = {}
rodando = True


def esta_online(ip, tentativas=3):
    for _ in range(max(1, int(tentativas))):
        try:
            resposta = ping(ip, timeout=1)
            if resposta is not None and resposta is not False:
                return True
        except Exception:
            pass
    return False


def monitorar():
    with ThreadPoolExecutor(max_workers=32) as pool:
        while rodando:
            dispositivos = [d for d in config["dispositivos"] if d.get("ativo", True)]
            resultados = list(pool.map(lambda d: esta_online(d["ip"], d.get("tentativas", 3)), dispositivos))
            agora = time.time()
            for disp, online in zip(dispositivos, resultados):
                st = estado.setdefault(disp["id"], {"online": None, "primeira_falha": None, "texto": ""})
                if online:
                    st.update(online=True, primeira_falha=None, texto="Online")
                else:
                    if st["primeira_falha"] is None:
                        st["primeira_falha"] = agora
                    st["online"] = False
                    st["texto"] = f"Fora do ar há {formatar_tempo(agora - st['primeira_falha'])}"
            app.after(0, atualizar_tela)
            for _ in range(INTERVALO_VERIFICACAO * 2):
                if not rodando:
                    return
                time.sleep(0.5)


def formatar_tempo(segundos):
    m, s = divmod(int(segundos), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


def descrever_tolerancia(d):
    unidade = d.get("tolerancia_unidade", "segundos")
    valor = int(d.get("tolerancia", 30)) // UNIDADES_TEMPO.get(unidade, 1)
    return f"{valor} {unidade if valor != 1 else unidade[:-1]}"


def alerta_deve_tocar(d, segundos_em_alerta):
    """repeticoes = 0: contínuo, toca até o dispositivo voltar.
    repeticoes > 0: toca 'duracao_alerta' segundos, espera 'intervalo_alerta' e repete esse número de vezes."""
    repeticoes = obter_repeticoes(d)
    if repeticoes == 0:
        return True
    duracao = max(1, int(d.get("duracao_alerta", 60)))
    ciclo = duracao + max(0, int(d.get("intervalo_alerta", 600)))
    if segundos_em_alerta >= repeticoes * ciclo:
        return False
    return segundos_em_alerta % ciclo < duracao


def obter_repeticoes(d):
    # Cadastros antigos com modo "Contínuo" (ou sem o campo) contam como contínuo (0)
    if d.get("modo_alerta") == "Contínuo" or "repeticoes" not in d:
        return 0
    return max(0, int(d.get("repeticoes", 0)))


def dispositivos_em_alerta():
    """Lista de (dispositivo, tocando) para os que passaram do tempo de tolerância."""
    agora = time.time()
    alerta = []
    for d in config["dispositivos"]:
        st = estado.get(d["id"])
        if not d.get("ativo", True) or not st or st["primeira_falha"] is None:
            continue
        em_alerta_ha = agora - st["primeira_falha"] - int(d.get("tolerancia", 30))
        if em_alerta_ha >= 0:
            alerta.append((d, alerta_deve_tocar(d, em_alerta_ha)))
    return alerta


# --- BARRA VERMELHA DE ALERTA ---
janela_barra = None


def mostrar_barra(texto, cor="#D32F2F"):
    global janela_barra
    if janela_barra is not None and janela_barra.winfo_exists():
        janela_barra.lbl.configure(text=texto)
        janela_barra.frame.configure(fg_color=cor)
        return
    janela_barra = ctk.CTkToplevel(app)
    janela_barra.overrideredirect(True)
    janela_barra.attributes("-topmost", True)
    transparente = "#000001"
    janela_barra.attributes("-transparentcolor", transparente)
    janela_barra.configure(fg_color=transparente)
    janela_barra.attributes("-alpha", 0.92)
    largura_tela = app.winfo_screenwidth()
    largura = int(largura_tela * 0.7)
    janela_barra.geometry(f"{largura}x55+{(largura_tela - largura) // 2}+10")
    frame = janela_barra.frame = ctk.CTkFrame(janela_barra, fg_color=cor, corner_radius=25)
    frame.pack(expand=True, fill="both", padx=2, pady=2)
    janela_barra.lbl = ctk.CTkLabel(frame, text=texto, font=("Arial", 20, "bold"), text_color="#FFFFFF")
    janela_barra.lbl.pack(expand=True, fill="both")
    # Deixa a barra "clicável através" para não atrapalhar o uso do PC
    janela_barra.update()
    hwnd = ctypes.windll.user32.GetParent(janela_barra.winfo_id())
    estilo = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
    ctypes.windll.user32.SetWindowLongW(hwnd, -20, estilo | 0x00080000 | 0x00000020)


def esconder_barra():
    global janela_barra
    if janela_barra is not None and janela_barra.winfo_exists():
        janela_barra.destroy()
    janela_barra = None


def sincronizar_alerta():
    global forcar_volume
    if _teste_em_andamento:
        return
    alerta = dispositivos_em_alerta()
    if not alerta:
        esconder_barra()
        parar_som()
        forcar_volume = False
        return
    nomes = [d["nome"] for d, _ in alerta]
    lista_nomes = ", ".join(nomes[:3]) + (f" e mais {len(nomes) - 3}" if len(nomes) > 3 else "")
    frase = config.get("mensagem_personalizada", "").strip()
    if config.get("mensagem_personalizada_ativa") and frase:
        texto = f"🚨 {frase}: {lista_nomes} 🚨"
    elif len(alerta) == 1:
        texto = f"🚨 {lista_nomes} PAROU DE FUNCIONAR 🚨"
    else:
        texto = f"🚨 PARARAM DE FUNCIONAR: {lista_nomes} 🚨"
    if any(tocando for _, tocando in alerta):
        mostrar_barra(texto)  # vermelha: alerta sonoro ativo
        forcar_volume = True
        tocar_som()
    else:
        # Dispositivo continua fora, mas está no intervalo entre alertas (ou acabaram as repetições):
        # a barra fica laranja e sem som, para não esconder o problema.
        mostrar_barra(texto + "  (som em pausa)", cor="#E65100")
        parar_som()
        forcar_volume = False


def ciclo_alerta():
    """Reavalia o alerta a cada segundo para respeitar duração/intervalo das repetições."""
    if rodando:
        sincronizar_alerta()
        app.after(1000, ciclo_alerta)


_teste_em_andamento = False


# --- INTERFACE ---
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")
app = ctk.CTk()
app.title("Alerta2UI")
app.geometry("760x560")
app.minsize(600, 400)
caminho_icone = resource_path(os.path.join("assets", "icone.ico"))
if os.path.exists(caminho_icone):
    try:
        app.iconbitmap(caminho_icone)
    except Exception:
        pass


def preparar_janela(janela, titulo, geometria):
    janela.title(titulo)
    janela.geometry(geometria)
    janela.resizable(False, False)
    janela.attributes("-topmost", True)
    janela.after(100, janela.grab_set)
    janela.after(150, janela.focus_force)


# --- DIÁLOGOS DE SENHA ---
dialogo_senha = None


def pedir_senha(motivo, ao_confirmar):
    """Pede a senha e chama ao_confirmar() se estiver correta. Cria a senha no primeiro uso."""
    global dialogo_senha
    if dialogo_senha is not None and dialogo_senha.winfo_exists():
        dialogo_senha.lift()
        dialogo_senha.focus_force()
        return
    criando = not config.get("senha_hash")
    janela = dialogo_senha = ctk.CTkToplevel(app)
    preparar_janela(janela, "Alerta2UI - Senha", "340x290" if criando else "340x220")
    largura, altura = app.winfo_screenwidth(), app.winfo_screenheight()
    janela.geometry(f"+{(largura - 340) // 2}+{(altura - 290) // 2}")

    ctk.CTkLabel(janela, text="🔒 " + ("Crie uma senha de acesso" if criando else motivo),
                 font=("Arial", 16, "bold")).pack(pady=(20, 10))
    e_senha = ctk.CTkEntry(janela, show="*", width=240, placeholder_text="Senha")
    e_senha.pack(pady=4)
    e_confirma = None
    if criando:
        e_confirma = ctk.CTkEntry(janela, show="*", width=240, placeholder_text="Confirme a senha")
        e_confirma.pack(pady=4)
    lbl_erro = ctk.CTkLabel(janela, text="", text_color=COR_OFFLINE)
    lbl_erro.pack()
    janela.after(200, e_senha.focus_set)

    def confirmar(event=None):
        senha = e_senha.get()
        if criando:
            if len(senha) < 4:
                lbl_erro.configure(text="Use pelo menos 4 caracteres.")
                return
            if senha != e_confirma.get():
                lbl_erro.configure(text="As senhas não conferem.")
                return
            definir_senha(senha)
        elif not senha_correta(senha):
            lbl_erro.configure(text="Senha incorreta!")
            e_senha.delete(0, "end")
            return
        janela.destroy()
        ao_confirmar()

    janela.bind("<Return>", confirmar)
    ctk.CTkButton(janela, text="Confirmar", width=240, command=confirmar).pack(pady=10)


# --- JANELA PRINCIPAL ---
topo = ctk.CTkFrame(app, fg_color="transparent")
topo.pack(fill="x", padx=16, pady=(14, 6))
ctk.CTkLabel(topo, text="📡 Dispositivos monitorados", font=("Arial", 20, "bold")).pack(side="left")
ctk.CTkButton(topo, text="⚙️ Configurações", width=130, fg_color="#455A64", hover_color="#37474F",
              command=lambda: pedir_senha("Senha para configurar", abrir_configuracoes)).pack(side="right", padx=(6, 0))
ctk.CTkButton(topo, text="➕ Adicionar", width=120, command=lambda: abrir_dispositivo()).pack(side="right")

lista = ctk.CTkScrollableFrame(app)
lista.pack(fill="both", expand=True, padx=16, pady=(0, 16))
linhas_ui = {}  # id -> (bolinha, label_status)


def reconstruir_lista():
    for w in lista.winfo_children():
        w.destroy()
    linhas_ui.clear()
    if not config["dispositivos"]:
        ctk.CTkLabel(lista, text="Nenhum dispositivo cadastrado. Clique em ➕ Adicionar.",
                     text_color=COR_TEXTO_SECUNDARIO).pack(pady=30)
        return
    for d in config["dispositivos"]:
        linha = ctk.CTkFrame(lista, fg_color=COR_CARTAO, corner_radius=10)
        linha.pack(fill="x", pady=4, padx=4)
        bolinha = ctk.CTkLabel(linha, text="●", font=("Arial", 22), text_color=COR_AGUARDANDO, width=30)
        bolinha.pack(side="left", padx=(10, 4))
        info = ctk.CTkFrame(linha, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, pady=6)
        ctk.CTkLabel(info, text=d["nome"], font=("Arial", 14, "bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(info, text=f"{d['ip']}  •  alerta após {descrever_tolerancia(d)} fora do ar",
                     font=("Arial", 11), text_color=COR_TEXTO_SECUNDARIO, anchor="w").pack(fill="x")
        status = ctk.CTkLabel(linha, text="Verificando...", font=("Arial", 12), width=160, anchor="e")
        status.pack(side="left", padx=8)
        chave = ctk.CTkSwitch(linha, text="", width=46, command=lambda d=d: alternar_monitoramento(d))
        if d.get("ativo", True):
            chave.select()
        chave.pack(side="left", padx=2)
        ctk.CTkButton(linha, text="✏️", width=36, fg_color="#546E7A", hover_color="#455A64",
                      command=lambda d=d: abrir_dispositivo(d)).pack(side="left", padx=2)
        ctk.CTkButton(linha, text="📋", width=36, fg_color="#5E35B1", hover_color="#4527A0",
                      command=lambda d=d: abrir_dispositivo(d, copia=True)).pack(side="left", padx=2)
        ctk.CTkButton(linha, text="🗑️", width=36, fg_color=COR_OFFLINE, hover_color="#B71C1C",
                      command=lambda d=d: remover_dispositivo(d)).pack(side="left", padx=(2, 10))
        linhas_ui[d["id"]] = (bolinha, status)
    atualizar_tela()


def alternar_monitoramento(d):
    d["ativo"] = not d.get("ativo", True)
    estado.pop(d["id"], None)  # ao reativar, começa a contar do zero
    salvar_config()
    atualizar_tela()


def atualizar_tela():
    em_alerta = {d["id"] for d, _ in dispositivos_em_alerta()}
    ativos = {d["id"] for d in config["dispositivos"] if d.get("ativo", True)}
    for id_, (bolinha, status) in linhas_ui.items():
        st = estado.get(id_)
        if id_ not in ativos:
            cor, texto = COR_AGUARDANDO, "⏸ Monitoramento pausado"
        elif not st or st["online"] is None:
            cor, texto = COR_AGUARDANDO, "Verificando..."
        elif st["online"]:
            cor, texto = COR_ONLINE, st["texto"]
        else:
            cor = COR_OFFLINE if id_ in em_alerta else COR_FORA_TOLERANCIA
            texto = st["texto"]
        try:
            bolinha.configure(text_color=cor)
            status.configure(text=texto, text_color=cor)
        except Exception:
            pass
    sincronizar_alerta()


def remover_dispositivo(d):
    if not messagebox.askyesno("Remover", f"Remover '{d['nome']}' ({d['ip']})?", parent=app):
        return
    config["dispositivos"] = [x for x in config["dispositivos"] if x["id"] != d["id"]]
    estado.pop(d["id"], None)
    salvar_config()
    reconstruir_lista()


def abrir_dispositivo(d=None, copia=False):
    """d=None: novo. d + copia=False: editar d. d + copia=True: novo dispositivo preenchido com os dados de d."""
    modelo = d or {}  # de onde vêm os valores iniciais dos campos
    if copia:
        d = None  # ao salvar, cria um novo em vez de alterar o original
    janela = ctk.CTkToplevel(app)
    titulo = "Copiar dispositivo" if copia else ("Editar dispositivo" if d else "Novo dispositivo")
    preparar_janela(janela, titulo, "380x340")

    ctk.CTkLabel(janela, text="Endereço IP ou hostname:").pack(anchor="w", padx=20, pady=(16, 0))
    e_ip = ctk.CTkEntry(janela, width=340)
    e_ip.pack(padx=20)

    ctk.CTkLabel(janela, text="Nome do dispositivo:").pack(anchor="w", padx=20, pady=(10, 0))
    linha_nome = ctk.CTkFrame(janela, fg_color="transparent")
    linha_nome.pack(fill="x", padx=20)
    e_nome = ctk.CTkEntry(linha_nome)
    e_nome.pack(side="left", fill="x", expand=True)

    def buscar_nome():
        ip = e_ip.get().strip()
        if not ip:
            lbl_erro.configure(text="Digite o IP primeiro.", text_color=COR_OFFLINE)
            return
        lbl_erro.configure(text="Buscando nome na rede...", text_color=COR_TEXTO_SECUNDARIO)

        def _buscar():
            try:
                nome = socket.gethostbyaddr(ip)[0].split(".")[0]
            except Exception:
                nome = None

            def _aplicar():
                if not janela.winfo_exists():
                    return
                if nome:
                    e_nome.delete(0, "end")
                    e_nome.insert(0, nome)
                    lbl_erro.configure(text="")
                else:
                    lbl_erro.configure(text="Nome não encontrado na rede. Digite manualmente.",
                                       text_color=COR_OFFLINE)
            app.after(0, _aplicar)
        threading.Thread(target=_buscar, daemon=True).start()

    ctk.CTkButton(linha_nome, text="🔍 Buscar nome", width=110, command=buscar_nome).pack(side="left", padx=(6, 0))

    def campo_tempo(pai, rotulo, chave, padrao_seg, padrao_unidade):
        """Linha com número + unidade (segundos/minutos/horas). Retorna função que lê (segundos, unidade)."""
        ctk.CTkLabel(pai, text=rotulo).pack(anchor="w", padx=20, pady=(8, 0))
        linha = ctk.CTkFrame(pai, fg_color="transparent")
        linha.pack(fill="x", padx=20)
        entrada = ctk.CTkEntry(linha, width=100)
        entrada.pack(side="left")
        combo = ctk.CTkComboBox(linha, values=list(UNIDADES_TEMPO), width=130, state="readonly")
        combo.pack(side="left", padx=(6, 0))
        unidade = modelo.get(chave + "_unidade", padrao_unidade)
        segundos = int(modelo.get(chave, padrao_seg))
        entrada.insert(0, str(segundos // UNIDADES_TEMPO.get(unidade, 1)))
        combo.set(unidade)

        def ler():
            return max(0, int(entrada.get().strip())) * UNIDADES_TEMPO[combo.get()], combo.get()
        return ler

    ler_tol = campo_tempo(janela, "Tempo fora do ar até alertar:", "tolerancia", 30, "segundos")

    ctk.CTkLabel(janela, text="Tentativas de resposta (ping) antes de considerar fora do ar:").pack(
        anchor="w", padx=20, pady=(8, 0))
    e_tentativas = ctk.CTkEntry(janela, width=100)
    e_tentativas.insert(0, str(modelo.get("tentativas", 3)))
    e_tentativas.pack(anchor="w", padx=20)

    ctk.CTkLabel(janela, text="🔔 Alerta", font=("Arial", 14, "bold")).pack(anchor="w", padx=20, pady=(14, 0))
    ctk.CTkLabel(janela, text="Quantas vezes alertar (0 = contínuo, 1 = só uma vez):").pack(
        anchor="w", padx=20, pady=(4, 0))
    e_repeticoes = ctk.CTkEntry(janela, width=100)
    e_repeticoes.insert(0, str(obter_repeticoes(modelo) if modelo else 0))
    e_repeticoes.pack(anchor="w", padx=20)
    lbl_modo = ctk.CTkLabel(janela, text="", text_color=COR_TEXTO_SECUNDARIO, wraplength=380, justify="left")
    lbl_modo.pack(anchor="w", padx=20)

    # Duração e intervalo: só aparecem quando o alerta não é contínuo
    frame_repetir = ctk.CTkFrame(janela, fg_color="transparent")
    ler_duracao = campo_tempo(frame_repetir, "Duração de cada alerta sonoro:", "duracao_alerta", 60, "segundos")
    ler_intervalo = campo_tempo(frame_repetir, "Intervalo entre os alertas:", "intervalo_alerta", 600, "minutos")

    lbl_erro = ctk.CTkLabel(janela, text="", text_color=COR_OFFLINE)
    botao_salvar = ctk.CTkButton(janela, text="💾 Salvar")

    def ao_mudar_repeticoes(event=None):
        try:
            repeticoes = int(e_repeticoes.get().strip())
        except ValueError:
            repeticoes = 0
        frame_repetir.pack_forget()
        lbl_erro.pack_forget()
        botao_salvar.pack_forget()
        if repeticoes > 0:
            vezes = "1 vez" if repeticoes == 1 else f"{repeticoes} vezes"
            lbl_modo.configure(text=f"O som toca pela duração definida, pausa pelo intervalo e repete ({vezes}). "
                                    "A barra continua na tela (laranja) durante as pausas e depois do último alerta.")
            frame_repetir.pack(fill="x")
            janela.geometry("420x660")
        else:
            lbl_modo.configure(text="Contínuo: a barra vermelha e o som ficam ativos até o dispositivo voltar.")
            janela.geometry("420x520")
        lbl_erro.pack(pady=(6, 0))
        botao_salvar.pack(pady=10)

    e_repeticoes.bind("<KeyRelease>", ao_mudar_repeticoes)

    if modelo:
        e_nome.insert(0, modelo["nome"] + (" (cópia)" if copia else ""))
        e_ip.insert(0, modelo["ip"])
    janela.after(200, e_ip.focus_set)
    if copia:  # já deixa o IP selecionado, que normalmente é o que muda
        janela.after(250, lambda: e_ip.select_range(0, "end"))

    def salvar(event=None):
        nome, ip = e_nome.get().strip(), e_ip.get().strip()
        if not nome or not ip:
            lbl_erro.configure(text="Preencha nome e IP.", text_color=COR_OFFLINE)
            return
        try:
            tol, tol_und = ler_tol()
            duracao, duracao_und = ler_duracao()
            intervalo, intervalo_und = ler_intervalo()
            tentativas = int(e_tentativas.get().strip())
            repeticoes = int(e_repeticoes.get().strip())
        except ValueError:
            lbl_erro.configure(text="Tempos, tentativas e repetições devem ser números inteiros.",
                               text_color=COR_OFFLINE)
            return
        if tentativas < 1:
            lbl_erro.configure(text="Use pelo menos 1 tentativa.", text_color=COR_OFFLINE)
            return
        if repeticoes < 0:
            lbl_erro.configure(text="Quantidade de alertas não pode ser negativa.", text_color=COR_OFFLINE)
            return
        if repeticoes > 0 and duracao < 1:
            lbl_erro.configure(text="A duração do alerta deve ser maior que zero.", text_color=COR_OFFLINE)
            return
        dados = {
            "nome": nome, "ip": ip, "tentativas": tentativas,
            "tolerancia": tol, "tolerancia_unidade": tol_und,
            "duracao_alerta": duracao, "duracao_alerta_unidade": duracao_und,
            "intervalo_alerta": intervalo, "intervalo_alerta_unidade": intervalo_und,
            "repeticoes": repeticoes,
        }
        if d:
            if d["ip"] != ip:
                estado.pop(d["id"], None)
            d.pop("modo_alerta", None)  # campo antigo, não é mais usado
            d.update(dados)
        else:
            config["dispositivos"].append({"id": uuid.uuid4().hex, "ativo": True, **dados})
        salvar_config()
        reconstruir_lista()
        janela.destroy()

    janela.bind("<Return>", salvar)
    botao_salvar.configure(command=salvar)
    ao_mudar_repeticoes()


# --- BACKUP ---
# Formato .a2ubak: MAGICO + sal (16 bytes) + token Fernet (AES-128-CBC + HMAC-SHA256) de um .zip com
# config.json e o som. A chave vem da senha do backup (PBKDF2), então ele pode ser restaurado em
# qualquer computador, ao contrário do config.dat (DPAPI), que só abre no mesmo usuário/PC.
MAGICO_BACKUP = b"A2UIBAK1"
EXTENSAO_BACKUP = ".a2ubak"


def _chave_backup(senha, sal):
    chave = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, 390_000, dklen=32)
    return base64.urlsafe_b64encode(chave)


def backup_criptografado(origem):
    with open(origem, "rb") as f:
        return f.read(len(MAGICO_BACKUP)) == MAGICO_BACKUP


def exportar_backup(destino, senha):
    """Gera o backup criptografado com config (dispositivos, configurações, senha de acesso) e o som."""
    dados = dict(config)
    som = config.get("som", "")
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as zf:
        if som and os.path.exists(som):
            dados["som"] = os.path.basename(som)  # caminho muda de máquina para máquina
            zf.write(som, "som/" + os.path.basename(som))
        else:
            dados["som"] = ""
        zf.writestr("config.json", json.dumps(dados, indent=4, ensure_ascii=False))
    sal = os.urandom(16)
    token = Fernet(_chave_backup(senha, sal)).encrypt(memoria.getvalue())
    with open(destino, "wb") as f:
        f.write(MAGICO_BACKUP + sal + token)


def importar_backup(origem, senha=None):
    """Restaura um backup (.a2ubak criptografado, ou .zip antigo sem criptografia).
    Lança ValueError se o arquivo for inválido ou a senha estiver errada."""
    if backup_criptografado(origem):
        with open(origem, "rb") as f:
            conteudo = f.read()
        sal = conteudo[len(MAGICO_BACKUP):len(MAGICO_BACKUP) + 16]
        try:
            arquivo_zip = io.BytesIO(Fernet(_chave_backup(senha or "", sal)).decrypt(
                conteudo[len(MAGICO_BACKUP) + 16:]))
        except InvalidToken:
            raise ValueError("Senha do backup incorreta (ou arquivo corrompido).")
    elif zipfile.is_zipfile(origem):
        arquivo_zip = origem
    else:
        raise ValueError("Este arquivo não é um backup do Alerta2UI.")

    with zipfile.ZipFile(arquivo_zip) as zf:
        try:
            dados = json.loads(zf.read("config.json").decode("utf-8"))
        except KeyError:
            raise ValueError("Este arquivo não é um backup do Alerta2UI.")
        if not isinstance(dados.get("dispositivos"), list):
            raise ValueError("Backup inválido: lista de dispositivos não encontrada.")

        parar_som()
        nome_som = os.path.basename(dados.get("som", ""))
        dados["som"] = ""
        if nome_som and "som/" + nome_som in zf.namelist():
            for antigo in os.listdir(APPDATA_DIR):
                if antigo.startswith("som_alerta"):
                    os.remove(os.path.join(APPDATA_DIR, antigo))
            destino_som = os.path.join(APPDATA_DIR, "som_alerta" + os.path.splitext(nome_som)[1].lower())
            with open(destino_som, "wb") as f:
                f.write(zf.read("som/" + nome_som))
            dados["som"] = destino_som

    config.clear()
    config.update({"som": "", "volume": 100, "iniciar_windows": False, "dispositivos": [],
                   "senha_hash": "", "senha_sal": ""})
    config.update(dados)
    estado.clear()
    salvar_config()
    configurar_startup_windows(config.get("iniciar_windows", False))
    reconstruir_lista()


def pedir_senha_backup(pai, criando):
    """Janela modal que pede a senha do backup. Retorna a senha, ou None se cancelar."""
    resultado = {"senha": None}
    janela = ctk.CTkToplevel(pai)
    preparar_janela(janela, "Senha do backup", "360x290" if criando else "360x220")
    texto = ("Crie uma senha para proteger o backup.\nEla será pedida ao restaurar."
             if criando else "Digite a senha deste backup:")
    ctk.CTkLabel(janela, text="🔒 " + texto, font=("Arial", 13, "bold"), justify="left").pack(pady=(18, 10))
    e_senha = ctk.CTkEntry(janela, show="*", width=260, placeholder_text="Senha do backup")
    e_senha.pack(pady=4)
    e_conf = None
    if criando:
        e_conf = ctk.CTkEntry(janela, show="*", width=260, placeholder_text="Confirme a senha")
        e_conf.pack(pady=4)
    lbl_erro = ctk.CTkLabel(janela, text="", text_color=COR_OFFLINE)
    lbl_erro.pack()
    janela.after(200, e_senha.focus_set)

    def confirmar(event=None):
        senha = e_senha.get()
        if criando and len(senha) < 4:
            lbl_erro.configure(text="Use pelo menos 4 caracteres.")
            return
        if criando and senha != e_conf.get():
            lbl_erro.configure(text="As senhas não conferem.")
            return
        resultado["senha"] = senha
        janela.destroy()

    janela.bind("<Return>", confirmar)
    ctk.CTkButton(janela, text="Confirmar", width=260, command=confirmar).pack(pady=10)
    pai.wait_window(janela)
    try:
        pai.grab_set()  # devolve o foco modal para a janela de configurações
    except Exception:
        pass
    return resultado["senha"]


def abrir_configuracoes():
    janela = ctk.CTkToplevel(app)
    preparar_janela(janela, "Configurações", "440x680")
    som_escolhido = {"caminho": config.get("som", "")}

    ctk.CTkLabel(janela, text="🔊 Som do alerta", font=("Arial", 14, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
    lbl_som = ctk.CTkLabel(janela, anchor="w", text_color=COR_TEXTO_SECUNDARIO, wraplength=400)
    lbl_som.pack(fill="x", padx=20)

    def mostrar_nome_som():
        caminho = som_escolhido["caminho"]
        lbl_som.configure(text=os.path.basename(caminho) if caminho and os.path.exists(caminho)
                          else "Nenhum som selecionado")

    def escolher_som():
        janela.attributes("-topmost", False)
        arquivo = filedialog.askopenfilename(parent=janela, filetypes=FORMATOS_SOM)
        janela.attributes("-topmost", True)
        if arquivo:
            som_escolhido["caminho"] = arquivo
            mostrar_nome_som()

    def testar_som():
        global _teste_em_andamento
        _teste_em_andamento = True
        parar_som()
        tocar_som(som_escolhido["caminho"])
        app.after(4000, sincronizar_alerta_apos_teste)

    linha_som = ctk.CTkFrame(janela, fg_color="transparent")
    linha_som.pack(fill="x", padx=20, pady=6)
    ctk.CTkButton(linha_som, text="📂 Escolher arquivo", width=150, command=escolher_som).pack(side="left")
    ctk.CTkButton(linha_som, text="▶️ Testar", width=90, fg_color="#00695C", hover_color="#004D40",
                  command=testar_som).pack(side="left", padx=8)
    mostrar_nome_som()

    ctk.CTkLabel(janela, text="Volume do Windows durante o alerta (travado até o alerta acabar):",
                 wraplength=400, justify="left").pack(anchor="w", padx=20, pady=(14, 0))
    linha_vol = ctk.CTkFrame(janela, fg_color="transparent")
    linha_vol.pack(fill="x", padx=20)
    lbl_vol = ctk.CTkLabel(linha_vol, width=45)
    slider = ctk.CTkSlider(linha_vol, from_=10, to=100, number_of_steps=90,
                           command=lambda v: lbl_vol.configure(text=f"{int(v)}%"))
    slider.set(config.get("volume", 100))
    slider.pack(side="left", fill="x", expand=True)
    lbl_vol.configure(text=f"{int(slider.get())}%")
    lbl_vol.pack(side="left", padx=(8, 0))

    ctk.CTkLabel(janela, text="💬 Mensagem da barra de alerta", font=("Arial", 14, "bold")).pack(
        anchor="w", padx=20, pady=(14, 4))
    e_mensagem = ctk.CTkEntry(janela, width=400, placeholder_text="Ex.: POSSÍVEL ALAGAMENTO")
    e_mensagem.insert(0, config.get("mensagem_personalizada", ""))
    lbl_exemplo = ctk.CTkLabel(janela, text="", text_color=COR_TEXTO_SECUNDARIO, wraplength=400, justify="left")

    def atualizar_mensagem(event=None):
        ativa = chk_mensagem.get() == 1
        e_mensagem.configure(state="normal" if ativa else "disabled")
        frase = e_mensagem.get().strip()
        if ativa and frase:
            lbl_exemplo.configure(text=f"Na barra: 🚨 {frase}: Sala dos Servidores 🚨")
        else:
            lbl_exemplo.configure(text="Na barra: 🚨 Sala dos Servidores PAROU DE FUNCIONAR 🚨")

    chk_mensagem = ctk.CTkCheckBox(janela, text="Usar mensagem personalizada na barra",
                                   command=atualizar_mensagem)
    if config.get("mensagem_personalizada_ativa"):
        chk_mensagem.select()
    chk_mensagem.pack(anchor="w", padx=20, pady=(0, 4))
    e_mensagem.pack(padx=20)
    e_mensagem.bind("<KeyRelease>", atualizar_mensagem)
    lbl_exemplo.pack(anchor="w", padx=20)
    atualizar_mensagem()

    ctk.CTkLabel(janela, text="💾 Backup (dispositivos, configurações, senha e som)",
                 font=("Arial", 14, "bold")).pack(anchor="w", padx=20, pady=(14, 4))
    linha_backup = ctk.CTkFrame(janela, fg_color="transparent")
    linha_backup.pack(fill="x", padx=20)

    def acao_exportar():
        senha = pedir_senha_backup(janela, criando=True)
        if senha is None:
            return
        janela.attributes("-topmost", False)
        destino = filedialog.asksaveasfilename(
            parent=janela, defaultextension=EXTENSAO_BACKUP,
            filetypes=[("Backup Alerta2UI", "*" + EXTENSAO_BACKUP)],
            initialfile=f"Alerta2UI_backup_{date.today():%Y-%m-%d}{EXTENSAO_BACKUP}")
        janela.attributes("-topmost", True)
        if not destino:
            return
        try:
            exportar_backup(destino, senha)
            messagebox.showinfo("Backup", f"Backup salvo em:\n{destino}", parent=janela)
        except Exception as e:
            logging.error(f"Falha ao exportar backup: {e}")
            messagebox.showerror("Backup", f"Não foi possível salvar o backup:\n{e}", parent=janela)

    def acao_importar():
        janela.attributes("-topmost", False)
        origem = filedialog.askopenfilename(
            parent=janela, filetypes=[("Backup Alerta2UI", f"*{EXTENSAO_BACKUP} *.zip")])
        janela.attributes("-topmost", True)
        if not origem:
            return
        if not messagebox.askyesno(
                "Restaurar backup",
                "Isso vai SUBSTITUIR todos os dispositivos, configurações e a senha atuais "
                "pelos do backup.\n\nDeseja continuar?", parent=janela):
            return
        senha = None
        try:
            if backup_criptografado(origem):
                senha = pedir_senha_backup(janela, criando=False)
                if senha is None:
                    return
            importar_backup(origem, senha)
        except Exception as e:
            logging.error(f"Falha ao importar backup: {e}")
            messagebox.showerror("Backup", f"Não foi possível restaurar o backup:\n{e}", parent=janela)
            return
        janela.destroy()  # os campos desta janela ficaram desatualizados
        messagebox.showinfo("Backup", "Backup restaurado com sucesso.\n"
                                      "A senha agora é a mesma da máquina de origem.", parent=app)

    ctk.CTkButton(linha_backup, text="📤 Exportar backup", width=150, command=acao_exportar).pack(side="left")
    ctk.CTkButton(linha_backup, text="📥 Restaurar backup", width=150, fg_color="#00695C", hover_color="#004D40",
                  command=acao_importar).pack(side="left", padx=8)

    sw_windows = ctk.CTkSwitch(janela, text="Iniciar com o Windows")
    if startup_windows_ativo():  # lê o registro: o instalador também pode ter ativado
        sw_windows.select()
    sw_windows.pack(anchor="w", padx=20, pady=14)

    ctk.CTkLabel(janela, text="🔒 Alterar senha (deixe em branco para manter)",
                 font=("Arial", 14, "bold")).pack(anchor="w", padx=20, pady=(4, 4))
    e_nova = ctk.CTkEntry(janela, show="*", width=400, placeholder_text="Nova senha")
    e_nova.pack(padx=20, pady=2)
    e_conf = ctk.CTkEntry(janela, show="*", width=400, placeholder_text="Confirme a nova senha")
    e_conf.pack(padx=20, pady=2)
    lbl_erro = ctk.CTkLabel(janela, text="", text_color=COR_OFFLINE)
    lbl_erro.pack()

    def salvar():
        nova = e_nova.get()
        if nova or e_conf.get():
            if len(nova) < 4:
                lbl_erro.configure(text="A nova senha precisa de pelo menos 4 caracteres.")
                return
            if nova != e_conf.get():
                lbl_erro.configure(text="As senhas não conferem.")
                return
        caminho = som_escolhido["caminho"]
        if caminho and caminho != config.get("som") and os.path.exists(caminho):
            destino = os.path.join(APPDATA_DIR, "som_alerta" + os.path.splitext(caminho)[1].lower())
            try:
                parar_som()
                for antigo in os.listdir(APPDATA_DIR):
                    if antigo.startswith("som_alerta"):
                        os.remove(os.path.join(APPDATA_DIR, antigo))
                shutil.copy(caminho, destino)
                config["som"] = destino
            except Exception as e:
                logging.error(f"Falha ao copiar som: {e}")
                messagebox.showerror("Erro", f"Não foi possível copiar o arquivo de som:\n{e}", parent=janela)
                return
        if chk_mensagem.get() == 1 and not e_mensagem.get().strip():
            lbl_erro.configure(text="Digite a mensagem personalizada ou desmarque a opção.")
            return
        config["volume"] = int(slider.get())
        config["iniciar_windows"] = sw_windows.get() == 1
        config["mensagem_personalizada_ativa"] = chk_mensagem.get() == 1
        config["mensagem_personalizada"] = e_mensagem.get().strip()
        if nova:
            definir_senha(nova)
        salvar_config()
        configurar_startup_windows(config["iniciar_windows"])
        parar_som()
        sincronizar_alerta()
        janela.destroy()

    ctk.CTkButton(janela, text="💾 Salvar", fg_color="#2E7D32", hover_color="#1B5E20", command=salvar).pack(pady=8)


def sincronizar_alerta_apos_teste():
    global _teste_em_andamento
    _teste_em_andamento = False
    parar_som()
    sincronizar_alerta()


# --- BANDEJA DO SISTEMA (o programa vive aqui; a janela só abre com senha) ---
def mostrar_janela():
    app.deiconify()
    app.lift()
    app.focus_force()


def esconder_janela():
    for w in app.winfo_children():
        if isinstance(w, ctk.CTkToplevel) and w is not janela_barra:
            w.destroy()
    app.withdraw()


def encerrar_programa():
    global rodando, forcar_volume
    rodando = False
    forcar_volume = False
    parar_som()
    icone_bandeja.stop()
    app.after(500, app.quit)  # dá tempo da thread de volume restaurar o volume


def criar_icone_bandeja():
    try:
        imagem = Image.open(caminho_icone)
    except Exception:
        imagem = Image.new("RGB", (64, 64), color=(211, 47, 47))
    menu = pystray.Menu(
        pystray.MenuItem("Abrir", lambda icon, item: app.after(0, pedir_senha, "Senha para acessar", mostrar_janela),
                         default=True),
        pystray.MenuItem("Encerrar", lambda icon, item: app.after(0, pedir_senha, "Senha para encerrar",
                                                                  encerrar_programa)),
    )
    return pystray.Icon("Alerta2UI", imagem, "Alerta2UI - Monitorando", menu)


app.protocol("WM_DELETE_WINDOW", esconder_janela)

reconstruir_lista()
icone_bandeja = criar_icone_bandeja()
threading.Thread(target=icone_bandeja.run, daemon=True).start()
threading.Thread(target=monitorar, daemon=True).start()
threading.Thread(target=vigia_de_volume, daemon=True).start()
app.after(1000, ciclo_alerta)
app.withdraw()  # sempre inicia escondido, só no ícone da bandeja
app.mainloop()
