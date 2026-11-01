# Cassini talk — translation glossary (it, fr, es, pt-BR, pt-PT)

Decision D50 (i18n) with D45 (product name). Source catalog: `strings.json` (shared UX strings).
Italian follows the shipped iOS and Android translations (`ref/ios-it.json`, `ref/android-it.json`)
unless noted. Every app's translators use this file; when a term here and a single string disagree,
the glossary wins and the string gets fixed.

How to use it:

- Look up the term before translating a string that contains it. Use the same word every time,
  in every app, even where English varies ("write down" / "transcribe").
- Columns marked "button" give the label form. Sentences use the normal verb form for the
  register of that language (see "Voice and tone").
- Platform splits are written `GNOME / macOS`.
- "verify" means: copy the exact label the other product (GNOME, macOS, Chrome) shows in that locale.
  If it differs from this table, use the product's label and update this file.

---

## 1. Product, places and objects

| English | it | fr | es | pt-BR | pt-PT | Note |
|---|---|---|---|---|---|---|
| Cassini talk / Cassini | Cassini talk / Cassini | Cassini talk / Cassini | Cassini talk / Cassini | Cassini talk / Cassini | Cassini talk / Cassini | Never translated, never inflected. "talk" stays lowercase. No article in it/fr/es; pt-BR may use "o Cassini" when the sentence needs it. When grammar needs a gender, use **masculine** in all four (it: "Cassini si è chiuso", "Cassini è stato aggiornato"). iOS/Android Italian mix "si è chiusa"/"si è chiuso": masculine is the pick. Prefer phrasings that avoid agreement. |
| Library (the meeting list) | Libreria | Bibliothèque | Biblioteca | Biblioteca | Biblioteca | A named place: capitalise it like a proper noun when English does ("Back to Library" → "Torna alla Libreria"). Never "Archivio", "Documenti", "Réunions". |
| meeting | riunione (f) | réunion (f) | reunión (f) | reunião (f) | reunião (f) | The item in the Library. Never call a meeting a "file"/"fichier"/"archivo"/"arquivo" (except "Cassini file", see below). Feminine in all four: agree participles with it ("Ricevuta", "Reçue", "Recibida", "Recebida"). |
| Untitled meeting | Riunione senza titolo | Réunion sans titre | Reunión sin título | Reunião sem título | Reunião sem título | |
| browser meeting | riunione nel browser | réunion dans le navigateur | reunión en el navegador | reunião no navegador | reunião no navegador | A Zoom/Meet/etc. call in a browser tab. |
| recording (noun, the audio) | registrazione (f) | enregistrement (m) | grabación (f) | gravação (f) | gravação (f) | fr: masculine, unlike "réunion". |
| Recording (status, in progress) | Registrazione in corso | Enregistrement en cours | Grabando | Gravando | A gravar | Badge "Recording {liveBadge}": it "Registrazione {liveBadge}", fr "Enregistrement {liveBadge}", es "Grabando {liveBadge}", pt "Gravando {liveBadge}". |
| transcript | trascrizione (f) | transcription (f) | transcripción (f) | transcrição (f) | transcrição (f) | The text of a meeting. fr uses the same word for the text and the process. |
| Transcription (settings tab, process) | Trascrizione | Transcription | Transcripción | Transcrição | Transcrição | |
| write down / transcribe | trascrivere | transcrire | transcribir | transcrever | transcrever | English says "write down" to avoid jargon. In these four languages the transcribe verb is everyday language: always use it. Never "sbobinare", "mettre par écrit", "poner por escrito", "anotar". |
| Write it down (button) | Trascrivi | Transcrire | Transcribir | Transcrever | Transcrever | Matches mobile "Trascrivi". |
| Write it down again (button) | Trascrivi di nuovo | Transcrire à nouveau | Volver a transcribir | Transcrever de novo | Transcrever novamente | Matches mobile. |
| Transcribing… (badge) | In trascrizione… | Transcription en cours… | Transcribiendo… | Transcrevendo… | A transcrever… | With ETA: "In trascrizione, circa {n} minuti" / "Transcription, environ {n} minutes" / "Transcribiendo, quedan unos {n} minutos" / "Transcrevendo, faltam cerca de {n} minutos". |
| Writing down (details section) | Trascrizione | Transcription | Transcripción | Transcrição | Transcrição | |
| speaker (a person who talks) | voce (f) | intervenant (m) | hablante (m/f, generic masc.) | locutor (m) | orador (m) | it: mobile apps use "voce" throughout ("Voci", "Voce 1"); keep it. Android once uses "chi parla": do not, except as a verb phrase where "voce" reads badly. |
| Speaker {n} (unnamed speaker) | Voce {n} | Intervenant {n} | Hablante {n} | Locutor {n} | Orador {n} | Exact, everywhere (chips, rows, model description "Speaker 1, Speaker 2"). Never "Unknown speaker"/"Voce sconosciuta" for this label. |
| Speakers (heading) | Voci | Intervenants | Hablantes | Locutores | Oradores | |
| speakers (loudspeakers) | altoparlanti | haut-parleurs | altavoces | alto-falantes | altifalantes | **Trap:** `recording.systemMuted` "Your speakers are muted" means the audio output device, not people. |
| Me | Io | Moi | Yo | Eu | Eu | Speaker name of the microphone side and "What is recorded" choice. One word everywhere. "Recorded by me": "Registrate da me" / "Enregistrées par moi" / "Grabadas por mí" / "Gravadas por mim". |
| The others | Gli altri | Les autres | Los demás | Os outros | Os outros | The remote side. Never "audio di sistema", "audio du bureau", "audio del escritorio". |
| Both | Entrambi | Les deux | Ambos | Ambos | Ambos | |
| Your microphone | Il tuo microfono | Votre microphone | Tu micrófono | Seu microfone | O seu microfone | Never "ingresso microfono". |
| microphone | microfono | microphone | micrófono | microfone | microfone | fr: "microphone", not "micro", for consistency. |
| this computer’s sound | l’audio di questo computer | le son de cet ordinateur | el sonido de este equipo | o som deste computador | o som deste computador | What the computer plays. es: "equipo" is neutral across Spain and Latin America (avoid "ordenador"/"computadora"). |
| on this computer | su questo computer | sur cet ordinateur | en este equipo | neste computador | neste computador | Privacy lines. |
| saved voices | voci salvate | voix mémorisées | voces guardadas | vozes salvas | vozes guardadas | fr: **never "enregistrées"**, it collides with "enregistrement" (recording). |
| Remember this voice | Ricorda questa voce | Mémoriser cette voix | Recordar esta voz | Lembrar esta voz | Memorizar esta voz | fr "mémoriser" pairs with "voix mémorisées". |
| voiceprint | impronta vocale | empreinte vocale | huella de voz | impressão de voz | impressão vocal | The private biometric data. Never "biometria", "modèle vocal" (that is a speech model). |
| voice profile | profilo vocale | profil vocal | perfil de voz | perfil de voz | perfil de voz | "Delete {name}’s voice profile?" → "Eliminare il profilo vocale di {name}?" / "Supprimer le profil vocal de {name} ?" / "¿Eliminar el perfil de voz de {name}?" / "Excluir o perfil de voz de {name}?" |
| recognise (a saved voice) | riconoscere | reconnaître | reconocer | reconhecer | reconhecer | Mobile it uses "riconoscere" for this. |
| Recognised from (your) saved voices | Riconosciuta dalle (tue) voci salvate | Reconnu grâce à (vos) voix mémorisées | Reconocido por (tus) voces guardadas | Reconhecido pelas (suas) vozes salvas | Reconhecido a partir das (suas) vozes guardadas | Keep the "your" difference between the tooltip and the caption, as English does. it agrees with "voce" (f). |
| auto (tag) | auto | auto | auto | auto | auto | Kept as is, lowercase (iOS/Android Italian already do). |
| Likely (badge) | Probabile | Probable | Probable | Provável | Provável | |
| sample (of a voice) | campione | extrait | muestra | amostra | amostra | "Play a sample of {name}": "Riproduci un campione di {name}" etc. |
| confirmed speech | parlato confermato | parole confirmée | habla confirmada | fala confirmada | fala confirmada | "{duration} confirmed speech" → it "{duration} di parlato confermato" (mobile). |
| Speaks for {talkTime} in this meeting | Parla per {talkTime} in questa riunione | Parle pendant {talkTime} dans cette réunion | Habla durante {talkTime} en esta reunión | Fala por {talkTime} nesta reunião | Fala durante {talkTime} nesta reunião | |
| name (of a person) | nome | nom | nombre | nome | nome | |
| title (of a meeting) | titolo | titre | título | título | título | |
| note (Add a Note…) | nota | note | nota | nota | nota | |
| summary | riassunto | résumé | resumen | resumo | resumo | Filter "Has summary": "Con riassunto" / "Avec résumé" / "Con resumen" / "Com resumo". |
| Cassini file | file Cassini | fichier Cassini | archivo Cassini | arquivo Cassini | ficheiro Cassini | The exported/shared single file. Android "documento Cassini" is the mobile term for the same thing; desktop strings say "file", so use "file" here. |
| Markdown | Markdown | Markdown | Markdown | Markdown | Markdown | Format name, never translated. |
| folder | cartella | dossier | carpeta | pasta | pasta | |
| Library folder | Cartella della Libreria | Dossier de la bibliothèque | Carpeta de la biblioteca | Pasta da biblioteca | Pasta da Biblioteca | fr/es/pt: inside a phrase, lowercase is fine. |
| Music/Cassini | Music/Cassini | Music/Cassini | Music/Cassini | Music/Cassini | Music/Cassini | Literal path (HARMONY 4). Never use the localised XDG/Finder name ("Musica", "Musique", "Música"). |
| drive (Library offline) | unità | disque | unidad | unidade | unidade | |
| space (disk) | spazio | espace | espacio | espaço | espaço | "Only {n} minutes of space left" talks about recording time left. |
| Trash | Cestino | Corbeille | Papelera | Lixeira | Lixo | Capitalised, as GNOME and macOS do. iOS uses Apple’s "Eliminati di recente": that is iOS-only. |
| model (speech model) | modello (vocale) | modèle (vocal) | modelo (de voz) | modelo (de voz) | modelo (de voz) | |
| Tell speakers apart (model name) | Distingui le voci | Distinguer les intervenants | Distinguir hablantes | Distinguir locutores | Distinguir oradores | Quoted inside two status strings: the quoted text must match the name exactly. |
| speaker separation (details label) | Distinzione delle voci | Distinction des intervenants | Separación de hablantes | Separação de locutores | Separação de oradores | Noun form of the model name (it matches iOS "Distinzione delle voci"). |
| Recognise saved voices (model name) | Riconosci le voci salvate | Reconnaître les voix mémorisées | Reconocer voces guardadas | Reconhecer vozes salvas | Reconhecer vozes guardadas | Desktop names are verb phrases (unlike iOS "Riconoscimento delle voci salvate"); keep them verb phrases. |
| Included | Incluso | Inclus | Incluido | Incluído | Incluído | Model row. |
| In use for new meetings | In uso per le nuove riunioni | Utilisé pour les nouvelles réunions | En uso para reuniones nuevas | Em uso para novas reuniões | Em uso para novas reuniões | |
| On (model status, not a switch) | Attivo | Activé | Activado | Ativado | Ativado | |
| browser | browser | navigateur | navegador | navegador | navegador | it keeps the English word. |
| browser companion | estensione per il browser | extension de navigateur | extensión del navegador | extensão do navegador | extensão do navegador | English says "companion" to sound friendly; in these languages "extension" is the plain everyday word and what Chrome itself shows. Settings tab: "Estensione browser" / "Extension de navigateur" / "Extensión del navegador" / "Extensão do navegador". Never "host nativo", "native host". |
| network | rete | réseau | red | rede | rede | |
| update (noun) | aggiornamento | mise à jour | actualización | atualização | atualização | |
| notification | notifica | notification | notificación | notificação | notificação | |
| shortcut | scorciatoia | raccourci | atajo | atalho | atalho | |
| menu bar (macOS) | barra dei menu | barre des menus | barra de menús | barra de menus | barra de menus | |
| top bar (GNOME) | barra superiore | barre supérieure | barra superior | barra superior | barra superior | verify against gnome-shell. |
| Background Apps (GNOME) | — | — | — | — | — | Use gnome-shell’s own label in that locale; verify. |
| Files (GNOME app) / Finder | File / Finder | Fichiers / Finder | Archivos / Finder | Arquivos / Finder | Ficheiros / Finder | "Show in Files" → "Mostra in File" / "Afficher dans Fichiers" / "Mostrar en Archivos" / "Mostrar em Arquivos"; "Show in Finder" → "Mostra nel Finder" / "Afficher dans le Finder" / "Mostrar en el Finder" / "Mostrar no Finder". |
| Preferences / Settings (app) | Preferenze / Impostazioni | Préférences / Réglages | Preferencias / Ajustes | Preferências / Ajustes | Preferências / Definições | GNOME says Preferences, macOS says Settings (`settings.title` has a mac variant). Any "Settings" in a sentence about the app’s own settings follows the same split on that platform. |
| System Settings (macOS) / Settings (GNOME) | Impostazioni di Sistema / Impostazioni | Réglages Système / Paramètres | Ajustes del Sistema / Configuración | Ajustes do Sistema / Configurações | Definições do Sistema / Definições | The OS settings app. verify. |
| Extensions page (Chrome) | pagina Estensioni | page Extensions | página Extensiones | página Extensões | página Extensões | Chrome labels: verify against Chrome in that locale. |
| Developer mode (Chrome) | Modalità sviluppatore | Mode développeur | Modo de desarrollador | Modo do desenvolvedor | Modo de programador | verify. |
| Load unpacked (Chrome) | Carica estensione non pacchettizzata | Charger l’extension non empaquetée | Cargar descomprimida | Carregar sem compactação | Carregar expandida | verify. |
| address bar | barra degli indirizzi | barre d’adresse | barra de direcciones | barra de endereço | barra de endereço | |
| step (setup) | passo | étape | paso | etapa | passo | "Step {n}, done": "Passo {n}, completato" / "Étape {n}, terminée" / "Paso {n}, completado" / "Etapa {n}, concluída". |
| Today / Yesterday | Oggi / Ieri | Aujourd’hui / Hier | Hoy / Ayer | Hoje / Ontem | Hoje / Ontem | |
| Search | Cerca | Rechercher | Buscar | Buscar | Pesquisar | "Search every meeting": "Cerca in tutte le riunioni" / "Rechercher dans toutes les réunions" / "Buscar en todas las reuniones" / "Buscar em todas as reuniões". |
| Title / Name (field labels) | Titolo / Nome | Titre / Nom | Título / Nombre | Título / Nome | Título / Nome | |

## 2. Actions (buttons and menu items)

Button forms: it imperative (tu); fr, es, pt-BR infinitive. See section 4.

| English | it | fr | es | pt-BR | pt-PT | Note |
|---|---|---|---|---|---|---|
| Record | Registra | Enregistrer | Grabar | Gravar | Gravar | |
| Stop | Ferma | Arrêter | Detener | Parar | Parar | it: iOS uses "Ferma" ("Ferma e conserva i progressi"), the Android notification uses "Interrompi". Pick **"Ferma"**: "Interrompi" sounds like cutting something short. |
| Stop Recording (Mac menu, GNOME indicator only) | Ferma la registrazione | Arrêter l’enregistrement | Detener la grabación | Parar a gravação | Parar gravação | The only place this longer form is allowed. Never "Stop and Save". |
| Start recording | Avvia la registrazione | Commencer l’enregistrement | Empezar a grabar | Começar a gravar | Iniciar gravação | |
| Keep recording | Continua a registrare | Continuer l’enregistrement | Seguir grabando | Continuar gravando | Continuar a gravar | |
| Keep Recording in Background | Continua in background | Continuer en arrière-plan | Seguir en segundo plano | Continuar em segundo plano | Continuar em segundo plano | Shortened on purpose; the context already says recording. |
| Stop and keep it | Ferma e conserva | Arrêter et garder | Detener y conservar | Parar e manter | Parar e manter | |
| Play / Pause | Riproduci / Pausa | Lire / Pause | Reproducir / Pausar | Reproduzir / Pausar | Reproduzir / Pausa | |
| Play from {position} / Play from here | Riproduci da {position} / Riproduci da qui | Lire à partir de {position} / Lire à partir d’ici | Reproducir desde {position} / Reproducir desde aquí | Reproduzir a partir de {position} / Reproduzir daqui | Reproduzir a partir de {position} / Reproduzir a partir daqui | |
| Back / Forward 10 seconds | Indietro di 10 secondi / Avanti di 10 secondi | Reculer de 10 secondes / Avancer de 10 secondes | Retroceder 10 segundos / Avanzar 10 segundos | Voltar 10 segundos / Avançar 10 segundos | Recuar 10 segundos / Avançar 10 segundos | it from mobile. |
| Playback speed | Velocità di riproduzione | Vitesse de lecture | Velocidad de reproducción | Velocidade de reprodução | Velocidade de reprodução | |
| Cancel | Annulla | Annuler | Cancelar | Cancelar | Cancelar | |
| Undo | Annulla | Annuler | Deshacer | Desfazer | Desfazer | it/fr share the word with Cancel, as GNOME and macOS do; they never appear side by side. |
| Done | Fine | Terminé | Listo | Concluído | Concluído | it from mobile. |
| Close | Chiudi | Fermer | Cerrar | Fechar | Fechar | |
| Not now | Non ora | Plus tard | Ahora no | Agora não | Agora não | |
| Try again / Retry | Riprova | Réessayer | Reintentar | Tentar novamente | Tentar novamente | Same word for both English variants. In sentences: "riprova", "réessayez", "inténtalo de nuevo", "tente novamente". |
| Choose… / Choose folder | Scegli… / Scegli cartella | Choisir… / Choisir un dossier | Elegir… / Elegir carpeta | Escolher… / Escolher pasta | Escolher… / Escolher pasta | "Choose another folder": "Scegli un’altra cartella" / "Choisir un autre dossier" / "Elegir otra carpeta" / "Escolher outra pasta". |
| Delete | Elimina | Supprimer | Eliminar | Excluir | Eliminar | |
| Remove (a model) | Rimuovi | Supprimer | Quitar | Remover | Remover | |
| Forget / Forget everyone… | Dimentica / Dimentica tutti… | Oublier / Oublier tout le monde… | Olvidar / Olvidar a todos… | Esquecer / Esquecer todos… | Esquecer / Esquecer todos… | it from mobile. |
| Merge / Merge into… | Unisci / Unisci a… | Fusionner / Fusionner avec… | Fusionar / Fusionar con… | Mesclar / Mesclar com… | Juntar / Juntar a… | it from mobile. |
| Undo merge | Annulla unione | Annuler la fusion | Deshacer la fusión | Desfazer mesclagem | Desfazer junção | |
| Same person / Someone else | Stessa persona / Un’altra persona | Même personne / Quelqu’un d’autre | Misma persona / Otra persona | Mesma pessoa / Outra pessoa | Mesma pessoa / Outra pessoa | it from mobile. |
| Name the speakers | Dai un nome alle voci | Nommer les intervenants | Poner nombre a los hablantes | Nomear os locutores | Dar nome aos oradores | it pattern from mobile "Dai un nome a questa voce". |
| Save name | Salva il nome | Enregistrer le nom | Guardar nombre | Salvar nome | Guardar nome | fr "Enregistrer" is the OS word for Save; fine on a button whose object is clear. In sentences prefer "sauvegarder"/"garder" so it is never confused with recording. |
| Rename / Rename… | Rinomina / Rinomina… | Renommer / Renommer… | Renombrar / Renombrar… | Renomear / Renomear… | Mudar o nome / Mudar o nome… | Dialog title "Rename Meeting": "Rinomina riunione" / "Renommer la réunion" / "Renombrar reunión" / "Renomear reunião". |
| Move / Move… / Move Library | Sposta / Sposta… / Sposta la Libreria | Déplacer / Déplacer… / Déplacer la bibliothèque | Mover / Mover… / Mover la biblioteca | Mover / Mover… / Mover a biblioteca | Mover / Mover… / Mover a Biblioteca | |
| Move to Trash | Sposta nel Cestino | Mettre à la corbeille | Mover a la papelera | Mover para a Lixeira | Mover para o Lixo | Toast "“{title}” moved to Trash": it "“{title}” spostata nel Cestino", fr "« {title} » mise à la corbeille", es "“{title}” movida a la papelera", pt "“{title}” movida para a Lixeira" (agree with meeting, f). |
| Export / Export… | Esporta / Esporta… | Exporter / Exporter… | Exportar / Exportar… | Exportar / Exportar… | Exportar / Exportar… | |
| Share | Condividi | Partager | Compartir | Compartilhar | Partilhar | |
| Details / Recording details… | Dettagli / Dettagli della registrazione… | Détails / Détails de l’enregistrement… | Detalles / Detalles de la grabación… | Detalhes / Detalhes da gravação… | Detalhes / Detalhes da gravação… | |
| Copy details | Copia i dettagli | Copier les détails | Copiar detalles | Copiar detalhes | Copiar detalhes | |
| Open folder / Show | Apri cartella / Mostra | Ouvrir le dossier / Afficher | Abrir carpeta / Mostrar | Abrir pasta / Mostrar | Abrir pasta / Mostrar | |
| Open Cassini | Apri Cassini | Ouvrir Cassini | Abrir Cassini | Abrir o Cassini | Abrir o Cassini | |
| Open Settings / Open System Settings | Apri le impostazioni / Apri Impostazioni di Sistema | Ouvrir les réglages / Ouvrir Réglages Système | Abrir Ajustes / Abrir Ajustes del Sistema | Abrir Ajustes / Abrir Ajustes do Sistema | Abrir Definições / Abrir Definições do Sistema | Follow the Preferences/Settings split above on GNOME. |
| Check sound settings / Check | Controlla le impostazioni audio / Controlla | Vérifier les réglages du son / Vérifier | Revisar la configuración de sonido / Revisar | Verificar as configurações de som / Verificar | Verificar definições de som / Verificar | |
| Download | Scarica | Télécharger | Descargar | Baixar | Transferir | Noun "download": it "download" (m, invariable), fr "téléchargement", es "descarga", pt "download". |
| Set up | Configura | Configurer | Configurar | Configurar | Configurar | |
| Connect | Connetti | Connecter | Conectar | Conectar | Ligar | |
| Add / Add to {browser} | Aggiungi / Aggiungi a {browser} | Ajouter / Ajouter à {browser} | Añadir / Añadir a {browser} | Adicionar / Adicionar ao {browser} | Adicionar / Adicionar ao {browser} | pt-BR: browser names take "o" ("ao Chrome"). |
| Allow | Consenti | Autoriser | Permitir | Permitir | Permitir | Status "Allowed": "Consentito" / "Autorisé" / "Permitido" / "Permitido". |
| Copy | Copia | Copier | Copiar | Copiar | Copiar | "Copy folder location": "Copia il percorso della cartella" / "Copier l’emplacement du dossier" / "Copiar la ubicación de la carpeta" / "Copiar o local da pasta". |
| Update (verb) | Aggiorna | Mettre à jour | Actualizar | Atualizar | Atualizar | |
| Restart (the app) | Riavvia | Relancer | Reiniciar | Reiniciar | Reiniciar | fr: "Relancer" for an app ("Redémarrer" is for the computer). |
| Check for Updates… | Controlla aggiornamenti… | Rechercher des mises à jour… | Buscar actualizaciones… | Verificar atualizações… | Procurar atualizações… | |
| Quit Cassini | Esci da Cassini | Quitter Cassini | Salir de Cassini | Sair do Cassini | Sair do Cassini | |
| Settings… (menu item) | Impostazioni… | Réglages… | Ajustes… | Ajustes… | Definições… | macOS label; on GNOME use the Preferences word. |
| More actions / Main menu | Altre azioni / Menu principale | Plus d’actions / Menu principal | Más acciones / Menú principal | Mais ações / Menu principal | Mais ações / Menu principal | it "Altre azioni" from Android. |
| Back to Library | Torna alla Libreria | Retour à la bibliothèque | Volver a la biblioteca | Voltar à biblioteca | Voltar à Biblioteca | |

## 3. States, statuses and badges

| English | it | fr | es | pt-BR | pt-PT | Note |
|---|---|---|---|---|---|---|
| Saving… | Salvataggio… | Sauvegarde… | Guardando… | Salvando… | A guardar… | fr: never "Enregistrement…" (reads as "Recording…"). |
| Received | Ricevuta | Reçue | Recibida | Recebida | Recebida | Must equal the trailing word in `format.previewSubtitle.received`. |
| Interrupted at {position} | Interrotta a {position} | Interrompue à {position} | Interrumpida en {position} | Interrompida em {position} | Interrompida aos {position} | |
| Needs attention | Richiede attenzione | À vérifier | Requiere atención | Requer atenção | Requer atenção | |
| Downloaded / Not downloaded | Scaricato / Non scaricato | Téléchargé / Non téléchargé | Descargado / Sin descargar | Baixado / Não baixado | Transferido / Não transferido | Agree with "modello" (m). |
| Downloading, {percent}% | Download, {percent}% | Téléchargement, {percent} % | Descargando, {percent} % | Baixando, {percent}% | A transferir, {percent}% | See number typography in section 4. |
| Connected / Connected to {browser} | Connesso / Connesso a {browser} | Connecté / Connecté à {browser} | Conectado / Conectado a {browser} | Conectado / Conectado ao {browser} | Ligado / Ligado ao {browser} | |
| Connecting… / Setting up the connection… | Connessione… / Configurazione della connessione… | Connexion… / Configuration de la connexion… | Conectando… / Configurando la conexión… | Conectando… / Configurando a conexão… | A ligar… / A configurar a ligação… | |
| Not set up | Non configurata | Non configurée | Sin configurar | Não configurada | Não configurada | Agrees with the extension (f). |
| Named by you / Not named yet | Nome dato da te / Ancora senza nome | Nommé par vous / Pas encore nommé | Nombrado por ti / Sin nombre todavía | Nomeado por você / Ainda sem nome | Nome dado por si / Ainda sem nome | |
| Listening inside your browser | In ascolto nel browser | À l’écoute dans votre navigateur | Escuchando en tu navegador | Ouvindo no seu navegador | A ouvir no seu navegador | |
| Ready to record browser meetings | Pronto a registrare le riunioni nel browser | Prêt à enregistrer les réunions dans le navigateur | Listo para grabar reuniones en el navegador | Pronto para gravar reuniões no navegador | Pronto para gravar reuniões no navegador | Agrees with Cassini (m). |
| Not recorded | Non registrato | Non enregistré | Sin grabar | Não gravado | Não gravado | |

## 4. Voice and tone, per language

Shared rules for all four:

- **Sentence case** for everything: titles, headings, buttons, menu items, tabs. English Title Case
  ("Rename Meeting", "Stop Recording", "Check for Updates…", "Copy Address and Open {browser}") becomes
  sentence case. Capitalise only the first word, proper nouns (Cassini, Chrome, GNOME) and the
  named places Library/Trash where this glossary capitalises them.
- **Errors are lines with one action**: say what happened, then what is still safe. No "Error:",
  no blame, no exclamation marks, no raw error text, no "please" padding. Keep the reassurance
  sentence ("The audio is fine.", "Your recording so far is kept.").
- **"Couldn’t …" pattern** (use it for every such string):
  it "Impossibile …" · fr "Impossible de …" · es "No se pudo …" · pt-BR "Não foi possível …".
  Short status labels may use the participle: it "Download non riuscito", fr "Échec du téléchargement",
  es "Error en la descarga" (avoid "Error" in lines, fine in a 2-word status), pt-BR "Falha no download".
- **Confirmation dialogs**: title is a question with the verb in infinitive, primary button repeats the
  verb. it "Dimenticare tutte le voci salvate?" / "Dimentica tutti"; fr "Oublier toutes les voix mémorisées ?" /
  "Oublier tout le monde"; es "¿Olvidar todas las voces guardadas?" / "Olvidar a todos";
  pt-BR "Esquecer todas as vozes salvas?" / "Esquecer todos".
- **Ellipsis**: use the single character … exactly where English does (menu items that open a dialog,
  progress states). Never three dots.
- **Placeholders**: keep `{name}` exactly; reorder freely. Never build possessives by gluing text to a
  placeholder ("{name}’s" → "di {name}", "de {name}"). Avoid adjectives that must agree with `{name}`
  (gender unknown): rephrase ("Cassini riconoscerà {name}" rather than "{name} sarà riconosciuto").
  When the subject is `{title}` (a meeting), agree with "meeting" (feminine in all four).
- **Plurals**: give `one` and `other`, and also `many` (= the `other` text) for apps whose plural
  tables have it. In **fr and pt-BR, `one` covers 0 and 1**: write `{n}` in the `one` form
  ("{n} minute restante", "{n} reunião"), never a hard-coded "1". In it and es `one` is exactly 1.
  Every form except `one` must show `{n}`.
- **Units**: h, min, s stay as in English. MB/GB/KB stay in it, es, pt-BR; **fr uses Mo, Go, Ko**.
- **Keyboard**: keep Ctrl, Shift, Alt, ⌘, ⌥, ⇧, ⌫, F2 and letter keys as written. Key names follow
  the platform’s own labels in that locale: Enter → it "Invio", fr "Entrée", es "Intro", pt-BR "Enter";
  the GNOME "Delete" key label → it "Canc", fr "Suppr", es "Supr", pt-BR "Delete" (verify against GTK’s
  keyboard-label translations; macOS shows ⌘⌫).
- **Length**: keep UI strings about as short as English; buttons especially. If a button is much longer,
  drop articles first (it "Apri cartella"), then pick a shorter synonym from this glossary.
- **Language names** (`details.language.*`) are written in the UI language and, being standalone
  labels, start with a capital even though these languages lowercase them in running text:
  "Italian" → it "Italiano", fr "Italien", es "Italiano", pt-BR "Italiano"; "German" → it "Tedesco",
  fr "Allemand", es "Alemán", pt-BR "Alemão". Inside sentences (`models.whistle.description`) they stay
  lowercase after the first word: it "Inglese, tedesco, francese…".

### Italian (it)

- Informal **tu**, imperatives in the 2nd person singular: "Scegli un’altra cartella", "Riprova".
- Buttons: imperative ("Registra", "Ferma", "Esporta", "Scarica"), like iOS/Android.
- Quotes “ ” (curly, as the English source and iOS use), apostrophe ’ (U+2019) everywhere: "un’altra",
  "l’audio". Accented capitals as real characters: "È", never "E’".
- No space before punctuation. Percent without space: "{percent}%".
- English loanwords already used by the apps stay: browser, download, background, privacy, file
  (in "file Cassini"). Do not introduce new ones.
- Status lines often drop the verb: "In pausa", "In trascrizione", "Registrazione in corso".
- Errors: "Impossibile avviare la registrazione. La registrazione fin qui è salvata." Prefer
  "fin qui" (iOS majority) over "fatta finora".

### French (fr)

- **vous** throughout, imperatives with vous in sentences: "Choisissez un autre dossier",
  "Réessayez", "Autorisez Cassini à enregistrer le son de cet ordinateur."
- Buttons and menu items: infinitive ("Enregistrer", "Arrêter", "Exporter", "Télécharger", "Réessayer").
- French typography:
  - Narrow no-break space U+202F before ; ? ! and %, and inside guillemets: « {title} ».
  - No-break space U+00A0 before :.
  - Quotes « » (with U+202F inside) replace English “ ”. Apostrophe ’.
  - Examples: "Exporter « {title} »", "Supprimer le profil vocal de {name} ?", "{percent} %".
- Record vs save: "enregistrer/enregistrement" means **record**. For keeping things use "sauvegarder",
  "garder", "conserver", "mémoriser" (voices), except on Save buttons where "Enregistrer" is the OS word
  and the object is explicit.
- Errors: "Impossible de démarrer l’enregistrement." "Impossible de transcrire cette réunion. L’audio est intact."

### Spanish (es)

- **tú**, neutral international Spanish: no vosotros, no voseo, no regionalisms
  (equipo, not ordenador/computadora; "altavoces" for speakers).
- Buttons and menu items: infinitive ("Grabar", "Detener", "Exportar", "Descargar", "Reintentar").
  Sentences use tú imperatives: "Elige otra carpeta", "Inténtalo de nuevo".
- Opening ¿ and ¡ always. Quotes “ ” (UI convention, matching the English source). Apostrophes do
  not occur in Spanish.
- Percent with a no-break space U+00A0: "{percent} %" (RAE).
- Errors: "No se pudo empezar a grabar." "No se pudo transcribir esta reunión. El audio está bien."

### Portuguese, Brazil (pt-BR)

- **você**, imperatives in the você form in sentences: "Escolha outra pasta", "Tente novamente",
  "Permita que o Cassini grave o som deste computador."
- Buttons and menu items: infinitive ("Gravar", "Parar", "Exportar", "Baixar", "Tentar novamente").
- Brazilian vocabulary: baixar (not descarregar), arquivo (not ficheiro), tela, salvar, excluir,
  Lixeira, compartilhar, mesclar.
- Quotes “ ”. Percent without space: "{percent}%".
- Article with product and browser names when the sentence needs one: "o Cassini", "no Chrome".
- Errors: "Não foi possível iniciar a gravação." "Não foi possível transcrever esta reunião. O áudio está intacto."

### Portuguese, Portugal (pt-PT)

- **No "você"**: sentences use 3rd-person imperatives ("Escolha outra pasta", "Tente novamente",
  "Permita que o Cassini grave o som deste computador.") and impersonal or possessive forms
  ("o seu microfone", "Nome dado por si", "Pode parar qualquer gravação"). Where English has "you" as
  the subject, drop the pronoun or rephrase ("Antes de gravar", "Depois de esquecer uma voz…").
- Buttons and menu items: infinitive ("Gravar", "Parar", "Exportar", "Transferir", "Tentar novamente").
- European vocabulary, Acordo Ortográfico 1990 spelling as in Apple, Microsoft and GNOME pt-PT:
  transferir (not baixar), ficheiro (not arquivo), ecrã, guardar (not salvar), eliminar (not excluir),
  Lixo (not Lixeira), partilhar, juntar (not mesclar), pesquisar (not buscar), mudar o nome (not renomear),
  definições (not ajustes/configurações), aplicação (not aplicativo), utilizador, ligar/ligação (connect),
  separador (browser tab), aceder (not acessar), registado (not registrado), leitor (player),
  orador (speaker; not locutor), altifalantes (loudspeakers), Modo de programador.
- Progressive with "a" + infinitive, never the gerund: "A gravar", "A transcrever…", "A guardar…",
  "A transferir, {percent}%", "Os outros não estão a ser gravados".
- Quotes « » without inner spaces, replacing English “ ”: "Exportar «{title}»", "«{name}» já é uma voz
  guardada.", "Requer primeiro «Distinguir oradores»". Percent without space: "{percent}%".
- Article with product and browser names when the sentence needs one: "o Cassini", "no Chrome", "ao {browser}".
- Settings split: GNOME "Preferências", macOS "Definições"; the OS app is "Definições do Sistema" (macOS)
  / "Definições" (GNOME). Strings without a mac variant that point to the app's settings use the generic lowercase noun
  "definições" ("Abrir definições", "nas definições"), so they read correctly next to GNOME's "Preferências".
- **Plurals**: unlike pt-BR, `one` is exactly 1 (0 uses `other`). Still write `{n}` in `one`, and give
  `many` = `other`.
- Keys: Enter and Delete stay as written (verify against GTK's pt-PT keyboard labels).
- Errors: "Não foi possível iniciar a gravação." "Não foi possível transcrever esta reunião. O áudio está intacto."

## 5. Never translate

- Product names: **Cassini talk**, **Cassini** (also as the menu/a11y label `hidden.menu.a11y`).
- Model names: **Whistle**, **Parakeet Ultra**, **Nemotron** (translate only their descriptions).
- Brands and platforms: Chrome, Chromium, Edge, Brave, Firefox, Safari, GNOME, macOS, Mac, iOS,
  Android, Finder, Zoom, Google Meet, Microsoft Teams, GitHub.
- Format and file names: Markdown, Opus, M4A, WAV, MP3, JSON, and every file extension (.md, .opus, …),
  `{filename}` values, `meeting.json`, `people.json`, `models.json`.
- The literal path **Music/Cassini**.
- Browser addresses: `chrome://extensions/`, `edge://extensions/`, `brave://extensions/`.
- IDs and keys: string ids, `io.codemyriad.*` identifiers, extension IDs, licence names (GPLv3,
  GNU General Public License).
- Modifier keys and symbols: Ctrl, Shift, Alt, ⌘, ⌥, ⇧, ⌫, F2, and the shortcuts built from them
  (Ctrl+R, Ctrl+Alt+R, ⌘R, ⌥⌘R, Ctrl+L, ⌘⇧G).
- Placeholder names inside braces: `{name}`, `{n}`, `{title}`, `{browser}`, … (reordering is fine).
- The `auto` tag (kept as is by choice; it reads naturally in all four languages).
- Units h, min, s, MB, GB, KB (except fr Mo, Go, Ko).
- Strings marked `galleryOnly` or `*.when` ("never shown to a person") may be translated for
  completeness but carry no terminology weight.

## 6. Italian choices where iOS and Android differ

| Topic | iOS | Android | Pick |
|---|---|---|---|
| Stop (recording) | "Ferma" (Ferma e conserva i progressi, Ferma il campione) | notification "Interrompi" | **Ferma** |
| Cassini’s gender | "si è chiusa" and "si è chiuso" | same mix | **masculine**, avoid when possible |
| speaker | "voce" | "voce", once "distinguere chi parla" | **voce** |
| Trash | "Eliminati di recente" (Apple) | "cestino" (lowercase) | desktop **Cestino** (capitalised, as GNOME/macOS) |
| "so far" | "fin qui" | "fin qui" / "fatta finora" | **fin qui** |
| Recording stopped | "Registrazione interrotta" / "Fermata" | same | status label: **Registrazione interrotta**; Stop button stays "Ferma" |
