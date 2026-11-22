class TerrainError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TerrainLodStitcher:
    """Terrain chunk LOD meshes, border stitching and physics heights."""

    def __init__(self, chunk_size=4, split_distance=32.0,
                 height_epsilon=0.01):
        self.chunk_size = chunk_size
        self.split_distance = split_distance
        self.height_epsilon = height_epsilon

    def set_heightmap(self, heights, version=1):
        raise NotImplementedError

    def set_hole_mask(self, mask):
        raise NotImplementedError

    def build_chunk(self, chunk_x, chunk_y, lod=0):
        raise NotImplementedError

    def stitch(self, chunk_a, chunk_b):
        raise NotImplementedError

    def border_heights(self, chunk_id):
        raise NotImplementedError

    def render_height(self, x, y):
        raise NotImplementedError

    def physics_height(self, x, y):
        raise NotImplementedError

    def normal_at(self, x, y):
        raise NotImplementedError

    def set_view(self, x, z):
        raise NotImplementedError

    def morph_factor(self, distance):
        raise NotImplementedError

    def apply_rebuild(self, chunk_id, result_version):
        raise NotImplementedError

    def chunk_state(self, chunk_id):
        raise NotImplementedError

    def visible_chunks(self, camera_chunk):
        raise NotImplementedError

    def is_hole(self, x, y):
        raise NotImplementedError

    def index_type(self, vertex_count):
        raise NotImplementedError

    def skirt_vertices(self, chunk_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
