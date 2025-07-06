import time
import os
import threading
from pathlib import Path
from flask import Flask, Response, stream_with_context, render_template, url_for, request, abort, jsonify
from werkzeug.utils import secure_filename
import mimetypes
import logging
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
import pickle
import json

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'your-secret-key-here'  # Changez ceci en production

# Formats audio et vidéo supportés
ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'ogg', 'wav', 'aac', 'm4a', 'flac'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'wmv', 'flv', 'mkv', 'webm'}
ALLOWED_EXTENSIONS = ALLOWED_AUDIO_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS

# Configuration YouTube API
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'
CLIENT_SECRETS_FILE = 'client_secrets.json'  # Fichier de configuration OAuth
CREDENTIALS_FILE = 'youtube_credentials.pickle'

# Configuration par défaut
DEFAULT_AUDIO_FILE = "sample.mp3"
CHUNK_SIZE = 8192
BITRATE_DELAY = 0.008

def get_all_audio_files():
    """Récupère tous les fichiers audio disponibles."""
    audio_files = []
    
    # Fichiers dans le répertoire courant
    for ext in ALLOWED_AUDIO_EXTENSIONS:
        audio_files.extend([str(f) for f in Path('.').glob(f'*.{ext}')])
    
    # Fichiers dans le dossier uploads
    upload_folder = Path(app.config['UPLOAD_FOLDER'])
    if upload_folder.exists():
        for ext in ALLOWED_AUDIO_EXTENSIONS:
            audio_files.extend([str(f) for f in upload_folder.glob(f'*.{ext}')])
    
    return sorted(audio_files)

def find_first_available_audio_file():
    """Trouve le premier fichier audio disponible."""
    if os.path.exists(DEFAULT_AUDIO_FILE):
        return DEFAULT_AUDIO_FILE
    
    audio_files = get_all_audio_files()
    return audio_files[0] if audio_files else None

def get_current_file_index(current_file):
    """Retourne l'index du fichier actuel dans la liste."""
    audio_files = get_all_audio_files()
    try:
        return audio_files.index(current_file)
    except ValueError:
        return 0

def get_next_file(current_file):
    """Retourne le fichier suivant dans la liste."""
    audio_files = get_all_audio_files()
    if not audio_files:
        return None
    
    try:
        current_index = audio_files.index(current_file)
        next_index = (current_index + 1) % len(audio_files)
        return audio_files[next_index]
    except ValueError:
        return audio_files[0]

def get_previous_file(current_file):
    """Retourne le fichier précédent dans la liste."""
    audio_files = get_all_audio_files()
    if not audio_files:
        return None
    
    try:
        current_index = audio_files.index(current_file)
        prev_index = (current_index - 1) % len(audio_files)
        return audio_files[prev_index]
    except ValueError:
        return audio_files[-1]

# Statistiques du serveur
server_stats = {
    'listeners': 0,
    'total_connections': 0,
    'current_file': find_first_available_audio_file(),
    'start_time': time.time(),
    'youtube_uploads': 0,
    'current_index': 0,
    'total_files': 0
}

def allowed_file(filename):
    """Vérifie si le fichier a une extension autorisée."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_audio_file(filename):
    """Vérifie si le fichier est un fichier audio."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_AUDIO_EXTENSIONS

def is_video_file(filename):
    """Vérifie si le fichier est un fichier vidéo."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS

def get_audio_mimetype(filename):
    """Détermine le type MIME du fichier audio."""
    mime_types = {
        '.mp3': 'audio/mpeg',
        '.ogg': 'audio/ogg',
        '.wav': 'audio/wav',
        '.aac': 'audio/aac',
        '.m4a': 'audio/mp4',
        '.flac': 'audio/flac'
    }
    ext = Path(filename).suffix.lower()
    return mime_types.get(ext, 'audio/mpeg')

def get_youtube_credentials():
    """Récupère les credentials YouTube API."""
    creds = None
    
    # Charger les credentials existants
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, 'rb') as token:
            creds = pickle.load(token)
    
    # Si les credentials ne sont pas valides, les renouveler
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Processus d'authentification OAuth
            if not os.path.exists(CLIENT_SECRETS_FILE):
                logger.error("Fichier client_secrets.json manquant pour l'authentification YouTube")
                return None
            
            flow = Flow.from_client_secrets_file(
                CLIENT_SECRETS_FILE,
                scopes=YOUTUBE_SCOPES
            )
            flow.redirect_uri = 'http://localhost:5000/youtube/callback'
            
            # Cette partie nécessite une interaction utilisateur
            # En production, vous devriez implémenter le flow OAuth complet
            return None
        
        # Sauvegarder les credentials
        with open(CREDENTIALS_FILE, 'wb') as token:
            pickle.dump(creds, token)
    
    return creds

def upload_to_youtube(file_path, title, description="", tags=None, privacy_status="private"):
    """Upload un fichier vers YouTube."""
    try:
        creds = get_youtube_credentials()
        if not creds:
            return {"error": "Authentification YouTube requise"}
        
        youtube = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=creds)
        
        # Configuration de l'upload
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags or [],
                'categoryId': '22'  # Catégorie "People & Blogs"
            },
            'status': {
                'privacyStatus': privacy_status
            }
        }
        
        # Détecter le type de fichier
        if is_video_file(file_path):
            media_body = MediaFileUpload(file_path, chunksize=-1, resumable=True)
        else:
            # Pour les fichiers audio, nous pourrions les convertir en vidéo
            # ou utiliser une image statique avec l'audio
            media_body = MediaFileUpload(file_path, chunksize=-1, resumable=True)
        
        # Effectuer l'upload
        insert_request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media_body
        )
        
        response = None
        error = None
        retry = 0
        
        while response is None:
            try:
                logger.info(f"Uploading {file_path} to YouTube...")
                status, response = insert_request.next_chunk()
                if response is not None:
                    if 'id' in response:
                        video_id = response['id']
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
                        server_stats['youtube_uploads'] += 1
                        logger.info(f"Upload terminé. Video ID: {video_id}")
                        return {
                            "success": True,
                            "video_id": video_id,
                            "video_url": video_url
                        }
                    else:
                        return {"error": f"Upload échoué: {response}"}
            except HttpError as e:
                if e.resp.status in [500, 502, 503, 504]:
                    retry += 1
                    if retry > 5:
                        return {"error": "Échec après plusieurs tentatives"}
                    time.sleep(2 ** retry)
                else:
                    return {"error": f"Erreur HTTP: {e}"}
            except Exception as e:
                return {"error": f"Erreur inattendue: {e}"}
        
    except Exception as e:
        logger.error(f"Erreur lors de l'upload YouTube: {e}")
        return {"error": str(e)}

def generate_audio(audio_file=None, loop=False):
    """Stream audio file en chunks pour simuler le streaming temps réel."""
    if audio_file is None:
        audio_file = server_stats['current_file']
    
    if not os.path.exists(audio_file):
        logger.error(f"Fichier audio non trouvé: {audio_file}")
        return
    
    try:
        while True:
            try:
                with open(audio_file, "rb") as f:
                    logger.info(f"Démarrage du streaming de {audio_file}")
                    
                    while True:
                        data = f.read(CHUNK_SIZE)
                        if not data:
                            break
                        
                        yield data
                        time.sleep(BITRATE_DELAY)
                    
                    if not loop:
                        break
                        
                    time.sleep(0.1)
                    
            except (IOError, OSError) as e:
                logger.error(f"Erreur lors de la lecture du fichier: {e}")
                break
                
    except Exception as e:
        logger.error(f"Erreur lors du streaming: {e}")
        return

def update_listener_count(change):
    """Met à jour le nombre d'auditeurs connectés."""
    server_stats['listeners'] += change
    if change > 0:
        server_stats['total_connections'] += 1
    server_stats['listeners'] = max(0, server_stats['listeners'])

def update_server_stats():
    """Met à jour les statistiques du serveur."""
    audio_files = get_all_audio_files()
    server_stats['total_files'] = len(audio_files)
    if server_stats['current_file']:
        server_stats['current_index'] = get_current_file_index(server_stats['current_file'])

@app.route("/")
def index():
    """Page principale avec lecteur audio et statistiques."""
    update_server_stats()
    
    stream_url = url_for("audio_stream")
    stats_url = url_for("stats")
    upload_url = url_for("upload_file")
    
    # Liste des fichiers disponibles
    audio_files = get_all_audio_files()
    video_files = []
    
    for ext in ALLOWED_VIDEO_EXTENSIONS:
        video_files.extend([str(f) for f in Path('.').glob(f'*.{ext}')])
    
    if Path(app.config['UPLOAD_FOLDER']).exists():
        for ext in ALLOWED_VIDEO_EXTENSIONS:
            video_files.extend([str(f) for f in Path(app.config['UPLOAD_FOLDER']).glob(f'*.{ext}')])
    
    return render_template("index.html", 
                         stream_url=stream_url, 
                         stats_url=stats_url,
                         upload_url=upload_url,
                         audio_files=audio_files,
                         video_files=video_files,
                         current_file=server_stats['current_file'],
                         current_index=server_stats['current_index'],
                         total_files=server_stats['total_files'])

@app.route("/stream")
@app.route("/stream/<path:filename>")
def audio_stream(filename=None):
    """Endpoint HTTP qui sert le flux audio continu."""
    loop = request.args.get('loop', 'false').lower() == 'true'
    
    if filename:
        filename = secure_filename(filename)
        if not allowed_file(filename) or not is_audio_file(filename):
            abort(400)
        
        audio_file = None
        possible_paths = [
            filename,
            os.path.join(app.config['UPLOAD_FOLDER'], filename)
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                audio_file = path
                break
        
        if not audio_file:
            return jsonify({'error': 'Fichier audio non trouvé'}), 404
    else:
        audio_file = server_stats['current_file']
    
    if not audio_file or not os.path.exists(audio_file):
        logger.error(f"Aucun fichier audio disponible pour le streaming")
        return jsonify({'error': 'Aucun fichier audio disponible'}), 404
    
    update_listener_count(1)
    server_stats['current_file'] = audio_file
    
    mimetype = get_audio_mimetype(audio_file)
    
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "icy-metaint": "0",
        "Access-Control-Allow-Origin": "*",
        "Content-Type": mimetype,
        "X-Content-Type-Options": "nosniff"
    }
    
    def generate_with_cleanup():
        try:
            yield from generate_audio(audio_file, loop)
        finally:
            update_listener_count(-1)
    
    return Response(
        stream_with_context(generate_with_cleanup()),
        mimetype=mimetype,
        headers=headers,
    )

@app.route("/stats")
def stats():
    """Endpoint pour récupérer les statistiques du serveur."""
    update_server_stats()
    uptime = time.time() - server_stats['start_time']
    
    stats_data = {
        'listeners': server_stats['listeners'],
        'total_connections': server_stats['total_connections'],
        'current_file': server_stats['current_file'],
        'youtube_uploads': server_stats['youtube_uploads'],
        'uptime': f"{uptime:.0f}s",
        'uptime_formatted': time.strftime("%H:%M:%S", time.gmtime(uptime)),
        'current_index': server_stats['current_index'],
        'total_files': server_stats['total_files']
    }
    
    return jsonify(stats_data)

@app.route("/upload", methods=['GET', 'POST'])
def upload_file():
    """Endpoint pour uploader des fichiers audio/vidéo."""
    if request.method == 'POST':
        if 'file' not in request.files:
            return jsonify({'error': 'Aucun fichier sélectionné'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'Aucun fichier sélectionné'}), 400
        
        if file and allowed_file(file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
            logger.info(f"Fichier uploadé: {filename}")
            
            # Déterminer le type de fichier
            file_type = "audio" if is_audio_file(filename) else "video"
            
            return jsonify({
                'success': True, 
                'filename': filename,
                'file_type': file_type,
                'file_path': file_path
            }), 200
        else:
            return jsonify({'error': 'Format de fichier non supporté'}), 400
    
    return render_template("upload.html")

@app.route("/youtube/upload", methods=['POST'])
def youtube_upload():
    """Endpoint pour uploader vers YouTube."""
    data = request.get_json()
    
    if not data or 'filename' not in data:
        return jsonify({'error': 'Nom de fichier requis'}), 400
    
    filename = secure_filename(data['filename'])
    title = data.get('title', filename)
    description = data.get('description', '')
    tags = data.get('tags', [])
    privacy_status = data.get('privacy_status', 'private')
    
    # Rechercher le fichier
    file_path = None
    possible_paths = [
        filename,
        os.path.join(app.config['UPLOAD_FOLDER'], filename)
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            file_path = path
            break
    
    if not file_path:
        return jsonify({'error': 'Fichier non trouvé'}), 404
    
    # Vérifier que c'est un fichier audio ou vidéo
    if not (is_audio_file(filename) or is_video_file(filename)):
        return jsonify({'error': 'Le fichier doit être audio ou vidéo'}), 400
    
    # Effectuer l'upload
    result = upload_to_youtube(file_path, title, description, tags, privacy_status)
    
    if 'error' in result:
        return jsonify(result), 500
    else:
        return jsonify(result), 200

@app.route("/youtube/auth")
def youtube_auth():
    """Initie le processus d'authentification YouTube."""
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return jsonify({'error': 'Configuration OAuth manquante'}), 500
    
    try:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=YOUTUBE_SCOPES
        )
        flow.redirect_uri = url_for('youtube_callback', _external=True)
        
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true'
        )
        
        # Stocker l'état dans la session (en production, utilisez une session sécurisée)
        return jsonify({
            'auth_url': authorization_url,
            'state': state
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/youtube/callback")
def youtube_callback():
    """Callback pour l'authentification YouTube."""
    try:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=YOUTUBE_SCOPES,
            state=request.args.get('state')
        )
        flow.redirect_uri = url_for('youtube_callback', _external=True)
        
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        
        # Sauvegarder les credentials
        with open(CREDENTIALS_FILE, 'wb') as token:
            pickle.dump(flow.credentials, token)
        
        return jsonify({'success': True, 'message': 'Authentification réussie!'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/files")
def list_files():
    """Endpoint pour lister les fichiers disponibles."""
    audio_files = get_all_audio_files()
    video_files = []
    
    for ext in ALLOWED_VIDEO_EXTENSIONS:
        video_files.extend([str(f) for f in Path('.').glob(f'*.{ext}')])
    
    if Path(app.config['UPLOAD_FOLDER']).exists():
        for ext in ALLOWED_VIDEO_EXTENSIONS:
            files = [str(f) for f in Path(app.config['UPLOAD_FOLDER']).glob(f'*.{ext}')]
            video_files.extend(files)
    
    return jsonify({
        'audio_files': audio_files,
        'video_files': video_files
    })

@app.route("/switch/<path:filename>")
def switch_audio(filename):
    """Endpoint pour changer le fichier audio en cours de diffusion."""
    filename = secure_filename(filename)
    
    if not allowed_file(filename) or not is_audio_file(filename):
        return jsonify({'error': 'Format de fichier audio non supporté'}), 400
    
    # Recherche du fichier
    audio_files = get_all_audio_files()
    
    for file_path in audio_files:
        file_name = os.path.basename(file_path)
        if file_name == filename or file_path == filename:
            server_stats['current_file'] = file_path
            server_stats['current_index'] = get_current_file_index(file_path)
            logger.info(f"Fichier audio changé vers: {file_path}")
            return jsonify({
                'success': True, 
                'current_file': file_path,
                'current_index': server_stats['current_index'],
                'total_files': len(audio_files)
            }), 200
    
    return jsonify({'error': f'Fichier audio non trouvé: {filename}'}), 404

@app.route("/next")
def next_track():
    """Passe au fichier suivant."""
    current_file = server_stats['current_file']
    if not current_file:
        return jsonify({'error': 'Aucun fichier en cours'}), 400
    
    next_file = get_next_file(current_file)
    if not next_file:
        return jsonify({'error': 'Aucun fichier suivant disponible'}), 404
    
    server_stats['current_file'] = next_file
    server_stats['current_index'] = get_current_file_index(next_file)
    
    logger.info(f"Passage au fichier suivant: {next_file}")
    
    return jsonify({
        'success': True,
        'current_file': next_file,
        'current_index': server_stats['current_index'],
        'total_files': len(get_all_audio_files())
    })

@app.route("/previous")
def previous_track():
    """Passe au fichier précédent."""
    current_file = server_stats['current_file']
    if not current_file:
        return jsonify({'error': 'Aucun fichier en cours'}), 400
    
    previous_file = get_previous_file(current_file)
    if not previous_file:
        return jsonify({'error': 'Aucun fichier précédent disponible'}), 404
    
    server_stats['current_file'] = previous_file
    server_stats['current_index'] = get_current_file_index(previous_file)
    
    logger.info(f"Passage au fichier précédent: {previous_file}")
    
    return jsonify({
        'success': True,
        'current_file': previous_file,
        'current_index': server_stats['current_index'],
        'total_files': len(get_all_audio_files())
    })

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Ressource non trouvée'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Erreur interne du serveur'}), 500

if __name__ == "__main__":
    # Création du dossier d'upload si nécessaire
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    
    # Vérification des prérequis YouTube
    if not os.path.exists(CLIENT_SECRETS_FILE):
        logger.warning("Fichier client_secrets.json manquant pour YouTube API")
        logger.info("Pour activer l'upload YouTube:")
        logger.info("1. Créez un projet sur Google Cloud Console")
        logger.info("2. Activez l'API YouTube Data API v3")
        logger.info("3. Créez des identifiants OAuth 2.0")
        logger.info("4. Téléchargez le fichier client_secrets.json")
    
    # Vérification de l'existence d'un fichier audio
    current_file = server_stats['current_file']
    if not current_file:
        logger.warning("Aucun fichier audio trouvé!")
        logger.info("Uploadez un fichier via /upload")
    else:
        logger.info(f"Fichier audio actuel: {current_file}")
    
    # Démarrage du serveur
    logger.info("Démarrage du serveur de diffusion audio/vidéo avec support YouTube...")
    logger.info(f"Formats audio supportés: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}")
    logger.info(f"Formats vidéo supportés: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}")
    logger.info("Interface web disponible sur: http://localhost:5000")
    
    app.run(debug=True, threaded=True, port=5000, host='0.0.0.0')