import os
import tensorflow as tf
import numpy as np
from plyfile import PlyData
from pose import LINEMOD_CAMERA_MATRIX


class MultiPoseLoss(object):
    """Multi-pose loss for a single-shot 6D object pose estimation
    architecture.

    # Arguments
        object_id: Str, ID of object to train in Linemod dataset,
            ex. powerdrill has an `object_id` of `08`.
        translation_priors: Array of shape `(num_boxes, 3)`,
            translation anchors.
        data_path: Str, root directory of Linemod dataset.
        target_num_points: Int,number of points of 3D model of object
            to consider for loss calculation.
        num_pose_dims: Int, number of pose dimensions.
        model_path: Directory containing ply files of Linemod objects.
        translation_scale_norm: Float, factor to change units.
            EfficientPose internally works with meter and if the
            dataset unit is mm for example, then this parameter
            should be set to 1000.

    # References
        - [EfficientPose: An efficient, accurate and scalable
           end-to-end 6D multi object pose estimation approach](
            https://arxiv.org/abs/2011.04307)
        - [EfficientPose](https://github.com/ybkscht/EfficientPose)
    """
    def __init__(self, object_id, translation_priors, data_path,
                 target_num_points=500, num_pose_dims=3, model_path='models/',
                 translation_scale_norm=1000):
        self.translation_priors = translation_priors
        self.num_pose_dims = num_pose_dims
        self.tz_scale = tf.convert_to_tensor(translation_scale_norm,
                                             dtype=tf.float32)
        model_data = load_model_data(data_path, model_path, object_id)
        model_points = get_vertices(model_data, key='vertex')
        self.model_points = filter_model_points(model_points,
                                                target_num_points)

    def compute_loss(self, y_true, y_pred):
        """Computes pose loss.

        # Arguments
            y_true: Tensor of shape '[batch_size, num_boxes, 11]'
                with correct labels.
            y_pred: Tensor of shape '[batch_size, num_boxes, 6]'
                with predicted inferences.

        # Returns
            Tensor with loss per sample in batch.
        """
        rotation_true = y_true[:, :, :self.num_pose_dims]
        rotation_pred = y_pred[:, :, :self.num_pose_dims]
        translation_true = y_true[:, :, 2 * self.num_pose_dims:2 *
                                  self.num_pose_dims + self.num_pose_dims]
        translation_pred = y_pred[:, :, self.num_pose_dims:]
        scale = y_true[0, 0, -1]
        translation_pred = compute_translation(translation_pred, scale,
                                               self.tz_scale,
                                               self.translation_priors)
        anchor_flags = y_true[:, :, -2]
        anchor_state = tf.cast(tf.math.round(anchor_flags), tf.int32)
        indices = tf.where(tf.equal(anchor_state, 1))

        rotation_true = tf.gather_nd(rotation_true, indices)
        rotation_pred = tf.gather_nd(rotation_pred, indices)
        rotation_true = rotation_true * np.pi
        rotation_pred = rotation_pred * np.pi
        axis_true, angle_true = separate_axis_from_angle(rotation_true)
        axis_pred, angle_pred = separate_axis_from_angle(rotation_pred)
        translation_true = tf.gather_nd(translation_true, indices)
        translation_pred = tf.gather_nd(translation_pred, indices)
        translation_true = translation_true[:, tf.newaxis, :]
        translation_pred = translation_pred[:, tf.newaxis, :]

        is_symmetric = y_true[:, :, self.num_pose_dims]
        is_symmetric = tf.gather_nd(is_symmetric, indices)
        is_symmetric = tf.cast(tf.math.round(is_symmetric), tf.int32)
        class_indices = y_true[:, :, self.num_pose_dims + 1]
        class_indices = tf.gather_nd(class_indices, indices)
        class_indices = tf.cast(tf.math.round(class_indices), tf.int32)

        selected_model_points = tf.gather(self.model_points,
                                          class_indices, axis=0)
        points_true_transformed = rotate(selected_model_points, axis_true,
                                         angle_true) + translation_true
        points_pred_transformed = rotate(selected_model_points, axis_pred,
                                         angle_pred) + translation_pred
        num_points = selected_model_points.shape[1]

        sym_indices = tf.where(tf.math.equal(is_symmetric, 1))
        sym_points_true = tf.reshape(tf.gather_nd(
            points_true_transformed, sym_indices), (-1, num_points, 3))
        sym_points_pred = tf.reshape(tf.gather_nd(
            points_pred_transformed, sym_indices), (-1, num_points, 3))

        asym_indices = tf.where(tf.math.not_equal(is_symmetric, 1))
        asym_points_true = tf.reshape(tf.gather_nd(
            points_true_transformed, asym_indices), (-1, num_points, 3))
        asym_points_pred = tf.reshape(tf.gather_nd(
            points_pred_transformed, asym_indices), (-1, num_points, 3))

        sym_distances = calc_sym_distances(sym_points_true, sym_points_pred)
        asym_distances = calc_asym_distances(asym_points_true,
                                             asym_points_pred)
        distances = tf.concat([sym_distances, asym_distances], axis=0)
        loss = tf.math.reduce_mean(distances)
        loss = tf.where(tf.math.is_nan(loss), tf.zeros_like(loss), loss)
        return loss


def load_model_data(data_path, model_path, object_id):
    """Loads model data stored in ply file.

    # Arguments
        data_path: Str, root directory of Linemod dataset.
        model_path: Directory containing ply files of Linemod objects.
        object_id: Str, ID of object to train in Linemod dataset,
            ex. powerdrill has an `object_id` of `08`.

    # Returns
        PlyData object containing parsed ply file contents.
    """
    object_filename = 'obj_{}.ply'.format(object_id)
    model_file_path = os.path.join(data_path, model_path, object_filename)
    return PlyData.read(model_file_path)


def get_vertices(model_data, key='vertex'):
    """Fetches vertices from model's ply file contents.

    # Arguments
        model_data: PlyData object containing parsed ply file contents.
        key: Str, containing the key name of the data to be fetched.

    # Returns
        Array of shape `[num_points, 3]` containing model vertices.
    """
    vertex = model_data[key][:]
    vertices = [vertex['x'], vertex['y'], vertex['z']]
    return np.stack(vertices, axis=-1)


def filter_model_points(model_points, target_num_points):
    """Filters/reduces model points to `target_num_points` points.

    # Arguments
        model_points: Array of shape `[num_points, 3]`
            containing model vertices.
        target_num_points: Int, number of points of 3D model of object
            to consider for loss calculation.

    # Returns
        Array of shape `[target_num_points, 3]` filtered model vertices.
    """
    num_points = model_points.shape[0]
    if num_points == target_num_points:
        points = model_points
    elif num_points < target_num_points:
        points = np.zeros((target_num_points, 3))
        points[:num_points, :] = model_points
    else:
        step_size = (num_points // target_num_points) - 1
        step_size = max(1, step_size)
        points = model_points[::step_size, :]
        points = points[np.newaxis, :target_num_points, :]
    return tf.convert_to_tensor(points)


def compute_translation(translation_pred_raw, scale, tz_scale,
                        translation_priors):
    """Computes x,y and z translation components from model's
    translation head output.

    # Arguments
        translation_pred_raw: Array of shape `(1, num_boxes, 3)`,
        scale: Array of shape `() containing translation scales`.
            containing model vertices.
        tz_scale: Array of shape `()`, containing scale along z axis.
        translation_priors:  Array of shape `(num_boxes, 3)` containing
            translation anchors.

    # Returns
        Array of shape `(1, num_boxes, 3)` computed translations with
        x, y and z components.
    """
    camera_matrix = tf.convert_to_tensor(LINEMOD_CAMERA_MATRIX)
    translation_pred = regress_translation(translation_pred_raw,
                                           translation_priors)
    return compute_tx_ty_tz(translation_pred, camera_matrix, tz_scale, scale)


def regress_translation(translation_raw, translation_priors):
    """Applies regression offset values to translation anchors
    to get the 2D translation center-point and Tz.

    # Arguments
        translation_raw: Array of shape `(1, num_boxes, 3)`,
        translation_priors:  Array of shape `(num_boxes, 3)` containing
            translation anchors.

    # Returns
        Array: of shape `(1, num_boxes, 3)`.
    """
    stride = translation_priors[:, -1]
    x = translation_priors[:, 0] + (translation_raw[:, :, 0] * stride)
    y = translation_priors[:, 1] + (translation_raw[:, :, 1] * stride)
    x, y = x[:, :, tf.newaxis], y[:, :, tf.newaxis]
    Tz = translation_raw[:, :, 2]
    Tz = Tz[:, :, tf.newaxis]
    return tf.concat([x, y, Tz], axis=-1)


def compute_tx_ty_tz(translation_xy_Tz, camera_matrix, tz_scale, scale):
    """Computes Tx, Ty and Tz components of the translation vector
    with a given 2D-point and the intrinsic camera parameters.

    # Arguments
        translation_xy_Tz: Array of shape `(num_boxes, 3)`,
        camera_matrix: Array: of shape `(3, 3)` camera parameter.
        tz_scale: Array of shape `()`, containing scale along z axis.
        scale: Array of shape `() containing translation scales`.

    # Returns
        Array of shape `(1, num_boxes, 3)` computed translations with
        x, y and z components.
    """
    fx, fy = camera_matrix[0, 0], camera_matrix[1, 1]
    px, py = camera_matrix[0, 2], camera_matrix[1, 2]
    x = translation_xy_Tz[:, :, 0] / scale
    y = translation_xy_Tz[:, :, 1] / scale
    tz = translation_xy_Tz[:, :, 2] * tz_scale
    x = x - px
    y = y - py
    tx = tf.math.multiply(x, tz) / fx
    ty = tf.math.multiply(y, tz) / fy
    tx, ty = tx[:, :, tf.newaxis], ty[:, :, tf.newaxis]
    tz = tz[:, :, tf.newaxis]
    return tf.concat([tx, ty, tz], axis=-1)


def separate_axis_from_angle(axis_angle):
    """Splits `axis_angle` into axis and angle component.

    # Arguments
        axis_angle: Array of shape `(target_num_points, 3)`,

    # Returns
        List, containing axis and angle components.
    """
    squared = tf.math.square(axis_angle)
    sum = tf.math.reduce_sum(squared, axis=-1)
    angle = tf.expand_dims(tf.math.sqrt(sum), axis=-1)
    axis = tf.math.divide_no_nan(axis_angle, angle)
    axis = axis[:, tf.newaxis, :]
    angle = angle[:, tf.newaxis, :]
    return [axis, angle]


def rotate(points, axis, angle):
    """Rotates `points` around `axis` with an `angle`.

    # Arguments
        points: Array of shape `(n, target_num_points, 3)`.
        axis: Array of shape `(target_num_points, 1, 3)`.
        angle: Array of shape `(target_num_points, 1, 1)`.

    # Returns
        Array, of shape `(n, target_num_points, 3)` rotated points.
    """
    cos_angle = tf.cos(angle)
    axis_dot_point = dot(axis, points)
    return (points * cos_angle + cross(axis, points) * tf.sin(angle)
            + axis * axis_dot_point * (1.0 - cos_angle))


def dot(vector1, vector2, axis=-1, keepdims=True):
    """computes dot product of two vectors `vector1` and `vector2`
    along an axis.

    # Arguments
        vector1: Array of shape `(target_num_points, 1, 3)`.
        vector2: Array of shape `(n, target_num_points, 3)`.
        axis: Int, axis along which sum is calculated.
        keepdims: Bool, retains array dimensions.

    # Returns
        Array, of shape `(n, target_num_points, 3)` dot product.
    """
    return tf.reduce_sum(input_tensor=vector1 * vector2,
                         axis=axis, keepdims=keepdims)


def cross(vector1, vector2, axis=-1):
    """computes cross product of two vectors `vector1` and `vector2`
    along an axis.

    # Arguments
        vector1: Array of shape `(target_num_points, 1, 3)`.
        vector2: Array of shape `(n, target_num_points, 3)`.
        axis: Int, axis along which cross product is calculated.

    # Returns
        Array, of shape `(n, target_num_points, 3)` cross product.
    """
    vector1_x, vector1_y,  = vector1[:, :, 0], vector1[:, :, 1]
    vector1_z = vector1[:, :, 2]
    vector2_x, vector2_y = vector2[:, :, 0], vector2[:, :, 1]
    vector2_z = vector2[:, :, 2]
    n_x = vector1_y * vector2_z - vector1_z * vector2_y
    n_y = vector1_z * vector2_x - vector1_x * vector2_z
    n_z = vector1_x * vector2_y - vector1_y * vector2_x
    return tf.stack((n_x, n_y, n_z), axis=axis)


def calc_sym_distances(sym_points_true, sym_points_pred):
    """computes the mean of pairwise point distances
    for objects that are symmetric.

    # Arguments
        sym_points_true: Array of shape `(1, target_num_points, 3)`.
        sym_points_pred: Array of shape `(1, target_num_points, 3)`.

    # Returns
        Array, of shape `()`.
    """
    sym_points_pred = sym_points_pred[:, :, tf.newaxis]
    sym_points_true = sym_points_true[:, tf.newaxis]
    norm = tf.norm(sym_points_pred - sym_points_true, axis=-1)
    distances = tf.reduce_min(norm, axis=-1)
    return tf.reduce_mean(distances, axis=-1)


def calc_asym_distances(asym_points_true, asym_points_pred):
    """computes the mean of pairwise point distances
    for objects that are asymmetric.

    # Arguments
        asym_points_true: Array of shape `(n, target_num_points, 3)`.
        asym_points_pred: Array of shape `(n, target_num_points, 3)`.

    # Returns
        Array, of shape `(target_num_points)`.
    """
    distances = tf.norm(asym_points_pred - asym_points_true, axis=-1)
    return tf.reduce_mean(distances, axis=-1)
