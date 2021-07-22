import os

from comet_ml import Experiment
import torchaudio
import torch
import torch.nn as nn
import torch.utils.data as data
import torch.optim as optim
from tqdm import trange

from exceptions import SpeechRecognizerNotTrained
from keys import COMET_API_KEY
from config import HYPER_PARAMS, DATA_DIR, CUDA, NUM_WORKERS, TRAIN_DATASET_URL, VALID_DATASET_URL, DEVICE, MODELS_DIR, \
    SAVED_INSTANCE_NAME
from preprocessing import preprocess, valid_audio_transforms
from model import SpeechRecognitionModel
from text_transformer import TextTransformer
from train import train, validate
from utils import greedy_decode
import torch.nn.functional as F

loaded_model = None


def load_saved_instance():
    global loaded_model
    saved_instance_path = os.path.join(MODELS_DIR, SAVED_INSTANCE_NAME)
    if os.path.isfile(saved_instance_path):
        checkpoint = torch.load(saved_instance_path, map_location=DEVICE)
        hyper_params = checkpoint['hparams']
        loaded_model = SpeechRecognitionModel(
            hyper_params['n_cnn_layers'], hyper_params['n_rnn_layers'], hyper_params['rnn_dim'],
            hyper_params['n_class'], hyper_params['n_feats'], hyper_params['stride'], hyper_params['dropout']
        ).to(DEVICE)
        loaded_model.load_state_dict(checkpoint['model_state_dict'])
        loaded_model.eval()
    else:
        raise SpeechRecognizerNotTrained()


@torch.no_grad()
def wav_to_text():
    global loaded_model
    if loaded_model is None:
        load_saved_instance()
    waveform, _ = torchaudio.load(os.path.join(DATA_DIR, 'test1.wav'))
    spectrogram = valid_audio_transforms(waveform).unsqueeze(0)
    decoded_predictions, _ = greedy_decode(F.log_softmax(loaded_model(spectrogram), dim=2))
    print(decoded_predictions)


def load_data():
    os.makedirs(DATA_DIR, exist_ok=True)

    train_dataset = torchaudio.datasets.LIBRISPEECH(DATA_DIR, url=TRAIN_DATASET_URL, download=True)
    valid_dataset = torchaudio.datasets.LIBRISPEECH(DATA_DIR, url=VALID_DATASET_URL, download=True)

    kwargs = {'num_workers': NUM_WORKERS, 'pin_memory': True} if CUDA else {}
    train_loader = data.DataLoader(dataset=train_dataset,
                                   batch_size=HYPER_PARAMS['batch_size'],
                                   shuffle=True,
                                   collate_fn=lambda x: preprocess(x, 'train'),
                                   **kwargs)
    valid_loader = data.DataLoader(dataset=valid_dataset,
                                   batch_size=HYPER_PARAMS['batch_size'],
                                   shuffle=False,
                                   collate_fn=lambda x: preprocess(x, 'valid'),
                                   **kwargs)
    return train_loader, valid_loader


def main():
    if COMET_API_KEY:
        experiment = Experiment(api_key=COMET_API_KEY, project_name="Speech Recognizer", parse_args=False)
    else:
        experiment = Experiment(api_key='dummy_key', disabled=True)

    experiment.log_parameters(HYPER_PARAMS)

    train_loader, valid_loader = load_data()

    model = SpeechRecognitionModel(
        HYPER_PARAMS['n_cnn_layers'], HYPER_PARAMS['n_rnn_layers'], HYPER_PARAMS['rnn_dim'],
        HYPER_PARAMS['n_class'], HYPER_PARAMS['n_feats'], HYPER_PARAMS['stride'], HYPER_PARAMS['dropout']
    ).to(DEVICE)

    print(model)
    print('Number of model parameters', sum([param.nelement() for param in model.parameters()]))

    optimizer = optim.AdamW(model.parameters(), HYPER_PARAMS['learning_rate'])
    criterion = nn.CTCLoss(blank=TextTransformer.BLANK_LABEL).to(DEVICE)
    scheduler = optim.lr_scheduler.OneCycleLR(optimizer, max_lr=HYPER_PARAMS['learning_rate'],
                                              steps_per_epoch=int(len(train_loader)),
                                              epochs=HYPER_PARAMS['epochs'],
                                              anneal_strategy='linear')

    step = 0
    previous_test_loss = float('inf')
    for epoch in trange(1, HYPER_PARAMS['EPOCHS'] + 1, leave=True, position=0):
        train(model, train_loader, criterion, optimizer, scheduler, epoch, step, experiment)
        current_test_loss = validate(model, valid_loader, criterion, step, experiment)
        print("Previous loss = {}\tCurrent loss = {}".format(previous_test_loss, current_test_loss))
        if current_test_loss < previous_test_loss:
            print("Saving model checkpoint...")
            torch.save({
                'epoch': epoch,
                'hparams': HYPER_PARAMS,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'scheduler_state_dict': scheduler.state_dict(),
                'loss': current_test_loss
            }, os.path.join(MODELS_DIR, SAVED_INSTANCE_NAME))
            previous_test_loss = current_test_loss


if __name__ == '__main__':
    wav_to_text()
