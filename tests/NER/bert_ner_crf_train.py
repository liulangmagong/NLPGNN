import tensorflow as tf
from fennlp.models import bert
from fennlp.optimizers import optim
from fennlp.tools import init_weights_from_checkpoint
from fennlp.datas.checkpoint import LoadCheckpoint
from fennlp.datas.dataloader import ZHTFWriter, NERLoader
from fennlp.metrics import Metric
from fennlp.metrics.crf import crf_log_likelihood, crf_decode

# 载入参数
load_check = LoadCheckpoint()
param, vocab_file, model_path = load_check.load_bert_param()

# 定制参数
param["batch_size"] = 2
param["maxlen"] = 80
param["label_size"] = 47


# 构建模型
class BERT_NER(tf.keras.Model):
    def __init__(self, param, **kwargs):
        super(BERT_NER, self).__init__(**kwargs)
        self.batch_size = param["batch_size"]
        self.maxlen = param["maxlen"]
        self.label_size = param["label_size"]
        self.bert = bert.BERT(param)
        self.dense = tf.keras.layers.Dense(self.label_size, activation="relu")

    def call(self, inputs, is_training=True):
        input_ids, token_type_ids, input_mask,outputs = tf.split(inputs, 4, 0)
        input_ids = tf.cast(tf.squeeze(input_ids, axis=0), tf.int64)
        token_type_ids = tf.cast(tf.squeeze(token_type_ids, axis=0), tf.int64)
        input_mask = tf.cast(tf.squeeze(input_mask, axis=0), tf.int64)
        outputs = tf.cast(tf.squeeze(outputs, axis=0), tf.int64)

        bert = self.bert([input_ids, token_type_ids, input_mask], is_training)
        sequence_output = bert.get_sequence_output()  # batch,sequence,768
        pre = self.dense(sequence_output)
        pre = tf.reshape(pre, [self.batch_size, self.maxlen, -1])
        log_likelihood, transition = crf_log_likelihood(pre, outputs, sequence_lengths=tf.reduce_sum(input_mask, 1))
        loss = tf.math.reduce_mean(-log_likelihood)
        predict, viterbi_score = crf_decode(pre, transition, sequence_length=tf.reduce_sum(input_mask, 1))
        return loss, predict

    def predict(self, inputs, outputs, is_training=False):
        loss, predict = self(inputs, outputs, is_training)
        return predict


model = BERT_NER(param)

model.build(input_shape = (4, param["batch_size"], param["maxlen"]))

model.summary()

# 构建优化器
optimizer_bert = optim.Adam(learning_rate=1e-5)
# optimizer_dense = tf.keras.optimizers.Adam(learning_rate=0.01)

# 初始化参数
init_weights_from_checkpoint(model,
                             model_path,
                             param["num_hidden_layers"],
                             pooler=False)

# 写入数据 通过check_exist=True参数控制仅在第一次调用时写入
writer = ZHTFWriter(param["maxlen"], vocab_file,
                    modes=["train"], check_exist=True)

ner_load = NERLoader(param["maxlen"], param["batch_size"], epoch=3)

# 训练模型
# 使用tensorboard
summary_writer = tf.summary.create_file_writer("./tensorboard")

# Metrics
f1score = Metric.SparseF1Score(average="macro", predict_sparse=True)
precsionscore = Metric.SparsePrecisionScore(average="macro", predict_sparse=True)
recallscore = Metric.SparseRecallScore(average="macro", predict_sparse=True)
accuarcyscore = Metric.SparseAccuracy(predict_sparse=True)

# 保存模型
checkpoint = tf.train.Checkpoint(model=model)
manager = tf.train.CheckpointManager(checkpoint, directory="./save",
                                     checkpoint_name="model.ckpt",
                                     max_to_keep=3)
# For train model
Batch = 0
for X, token_type_id, input_mask, Y in ner_load.load_train():
    with tf.GradientTape() as tape:
        loss, predict = model([X, token_type_id, input_mask,Y])
        # loss = mask_sparse_categotical_loss(Y, output, use_mask=True)

        f1 = f1score(Y, predict)
        precision = precsionscore(Y, predict)
        recall = recallscore(Y, predict)
        accuracy = accuarcyscore(Y, predict)
        if Batch % 21 == 0:
            print("Batch:{}\tloss:{:.4f}".format(Batch, loss.numpy()))
            print("Batch:{}\tacc:{:.4f}".format(Batch, accuracy))
            print("Batch:{}\tprecision{:.4f}".format(Batch, precision))
            print("Batch:{}\trecall:{:.4f}".format(Batch, recall))
            print("Batch:{}\tf1score:{:.4f}".format(Batch, f1))
            manager.save(checkpoint_number=Batch)

        with summary_writer.as_default():
            tf.summary.scalar("loss", loss, step=Batch)
            tf.summary.scalar("acc", accuracy, step=Batch)
            tf.summary.scalar("f1", f1, step=Batch)
            tf.summary.scalar("precision", precision, step=Batch)
            tf.summary.scalar("recall", recall, step=Batch)

    grads_bert = tape.gradient(loss, model.variables)
    # grads_dense = tape.gradient(loss, model.dense.variables)
    optimizer_bert.apply_gradients(grads_and_vars=zip(grads_bert, model.variables))
    # optimizer_dense.apply_gradients(grads_and_vars=zip(grads_dense, model.dense.variables))
    Batch += 1